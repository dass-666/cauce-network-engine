"""Detección en Windows 10/11 mediante PowerShell (módulo NetAdapter/NetTCPIP).

Solo LEE información: Get-NetAdapter, Get-NetIPAddress, Get-NetRoute,
Get-NetIPInterface. No requiere privilegios de administrador y no modifica
ninguna configuración.

ESTADO DE VERIFICACIÓN: probado con un fixture sintético y con una captura
REAL del runner Windows de GitHub (VM en Azure); falta una captura de un PC
físico con Wi‑Fi/4G. Ejecuta
``cauce discover --raw`` en tu equipo para guardar una captura real y
añadirla a ``tests/fixtures``.
"""
from __future__ import annotations

import base64
import binascii
import json
import subprocess
from typing import Any, Optional

from ..models import Connection, Kind, LinkState

PS_BODY = r"""
$ErrorActionPreference = 'Stop'
$adapters = @(Get-NetAdapter -IncludeHidden:$false | Select-Object Name, InterfaceDescription, ifIndex, Status, MacAddress, LinkSpeed, MediaType, PhysicalMediaType, NdisPhysicalMedium, Virtual, HardwareInterface)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object InterfaceIndex, IPAddress, PrefixLength, PrefixOrigin, AddressState)
$routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Select-Object ifIndex, NextHop, RouteMetric)
$ipif = @(Get-NetIPInterface -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object ifIndex, InterfaceMetric, ConnectionState)
$data = @{ adapters = $adapters; addresses = $addresses; routes = $routes; ipinterfaces = $ipif }
"""

# La salida de PowerShell 5.1 por tubería usa la página de códigos OEM (cp850 en
# Windows en español) y Python la leía como cp1252: "Conexión" llegaba como
# "Conexi¢n". Para no depender de la consola, el JSON se codifica en UTF-8 y se
# envía en Base64 (solo caracteres ASCII), que Python decodifica sin ambigüedad.
PS_EMIT = r"""
$json = ConvertTo-Json -Depth 4 -Compress -InputObject $data
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
"""

PS_SCRIPT = PS_BODY + PS_EMIT


# NDIS_PHYSICAL_MEDIUM (ntddndis.h)
_NDIS_MEDIUM = {
    0: Kind.UNKNOWN,     # Unspecified
    1: Kind.WIFI,        # WirelessLan
    8: Kind.MOBILE,      # WirelessWan
    9: Kind.WIFI,        # Native802_11
    10: Kind.TETHER,     # Bluetooth
    14: Kind.ETHERNET,   # 802_3
}

_GUEST_NIC_HINTS = ("hyper-v network adapter", "vmxnet3", "virtio", "red hat virtio", "intel(r) 82574l")
_VIRTUAL_HINTS = ("hyper-v", "virtualbox", "vmware", "wsl", "docker", "vethernet", "loopback", "kernel debug")
_VPN_HINTS = ("vpn", "tap-windows", "wireguard", "openvpn", "wintun", "tailscale", "zerotier", "fortinet", "cisco anyconnect")
_TETHER_HINTS = ("rndis", "remote ndis", "android", "iphone", "apple mobile device ethernet")
_MOBILE_HINTS = ("mobile broadband", "wwan", "lte", "5g", "qualcomm", "sierra wireless", "huawei mobile")


def _as_list(v: Any) -> list[dict[str, Any]]:
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return list(v)


def classify(adapter: dict[str, Any]) -> tuple[Kind, bool]:
    """Devuelve (tipo, es_virtual) a partir de un objeto de Get-NetAdapter."""
    desc = f"{adapter.get('Name', '')} {adapter.get('InterfaceDescription', '')}".lower()
    virtual = bool(adapter.get("Virtual")) or adapter.get("HardwareInterface") is False
    if any(h in desc for h in _GUEST_NIC_HINTS):
        # Tarjeta de red de un Windows que corre DENTRO de una máquina virtual: para ese
        # Windows es su salida real (hallado en la CI de GitHub, Windows Server en Azure).
        return Kind.ETHERNET, False
    if any(h in desc for h in _VPN_HINTS):
        return Kind.VPN, True
    if any(h in desc for h in _VIRTUAL_HINTS):
        return Kind.VIRTUAL, True
    if any(h in desc for h in _TETHER_HINTS):
        return Kind.TETHER, False
    medium = adapter.get("NdisPhysicalMedium")
    kind = _NDIS_MEDIUM.get(medium) if isinstance(medium, int) else None
    if kind in (None, Kind.UNKNOWN):
        pmt = str(adapter.get("PhysicalMediaType") or "").lower()
        if "802.11" in pmt or "wireless lan" in pmt:
            kind = Kind.WIFI
        elif "wireless wan" in pmt or any(h in desc for h in _MOBILE_HINTS):
            kind = Kind.MOBILE
        elif "802.3" in pmt or "ethernet" in desc:
            kind = Kind.ETHERNET
        else:
            kind = Kind.UNKNOWN
    if virtual and kind == Kind.UNKNOWN:
        kind = Kind.VIRTUAL
    return kind, virtual


def _link(status: Any) -> LinkState:
    s = str(status or "").lower()
    if s == "up":
        return LinkState.UP
    if s in ("disconnected", "down", "not present", "lowerlayerdown"):
        return LinkState.DOWN
    if s == "disabled":
        return LinkState.DISABLED
    return LinkState.UNKNOWN


def parse(payload: dict[str, Any]) -> list[Connection]:
    adapters = _as_list(payload.get("adapters"))
    addresses = _as_list(payload.get("addresses"))
    routes = _as_list(payload.get("routes"))
    ipif = {i.get("ifIndex"): i for i in _as_list(payload.get("ipinterfaces"))}

    addr_by_if: dict[int, list[str]] = {}
    for a in addresses:
        ip = a.get("IPAddress")
        if not ip or ip.startswith("169.254.") or ip.startswith("127."):
            continue  # APIPA = sin DHCP; no es una salida útil
        if str(a.get("AddressState", "Preferred")).lower() not in ("preferred", "4"):
            continue
        addr_by_if.setdefault(a.get("InterfaceIndex"), []).append(ip)

    gw_by_if: dict[int, tuple[str, Optional[int]]] = {}
    for r in routes:
        nh = r.get("NextHop")
        if not nh or nh == "0.0.0.0":
            continue
        idx = r.get("ifIndex")
        metric = r.get("RouteMetric")
        prev = gw_by_if.get(idx)
        if prev is None or (metric is not None and (prev[1] is None or metric < prev[1])):
            gw_by_if[idx] = (nh, metric)

    out: list[Connection] = []
    used_ids: set[str] = set()
    for ad in adapters:
        idx = ad.get("ifIndex")
        # Visto en un equipo real: dos adaptadores (vSwitch y NIC) con el mismo ifIndex.
        cid = f"win-{idx}"
        n = 2
        while cid in used_ids:
            cid = f"win-{idx}-{n}"
            n += 1
        used_ids.add(cid)
        kind, virtual = classify(ad)
        gw = gw_by_if.get(idx)
        metric = None
        if gw is not None:
            if_metric = (ipif.get(idx) or {}).get("InterfaceMetric")
            metric = (gw[1] or 0) + (if_metric or 0)  # Windows suma ambas métricas
        out.append(Connection(
            id=cid,
            name=str(ad.get("Name") or f"if{idx}"),
            description=str(ad.get("InterfaceDescription") or ""),
            kind=kind,
            link=_link(ad.get("Status")),
            if_index=idx,
            mac=ad.get("MacAddress"),
            ipv4=addr_by_if.get(idx, []),
            gateway=gw[0] if gw else None,
            route_metric=metric,
            is_virtual=virtual,
            source="windows",
        ))
    return out


def decode_ps_output(stdout: str) -> Any:
    """Decodifica la salida de PS_EMIT: Base64 de un JSON en UTF-8."""
    text = (stdout or "").strip()
    if not text:
        raise RuntimeError("PowerShell no devolvió datos")
    line = text.splitlines()[-1].strip()
    try:
        payload = base64.b64decode(line, validate=True)
    except (binascii.Error, ValueError) as e:
        raise RuntimeError(f"Salida de PowerShell inesperada: {text[:200]!r}") from e
    return json.loads(payload.decode("utf-8"))


def run_powershell_json(script: str, timeout: float) -> Any:
    """Ejecuta un script de solo lectura que termina con PS_EMIT y devuelve el JSON."""
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="ascii", errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell falló ({proc.returncode}): {(proc.stderr or '').strip()[:500]}")
    return decode_ps_output(proc.stdout)


class WindowsDiscovery:
    name = "windows"

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    def raw(self) -> dict[str, Any]:
        return run_powershell_json(PS_SCRIPT, self.timeout)

    def discover(self) -> list[Connection]:
        return parse(self.raw())
