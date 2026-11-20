"""Detección en Linux con ``ip -j`` (iproute2). Sirve para desarrollo y para
ejecutar el motor real en el entorno de CI; el objetivo de producción es Windows."""
from __future__ import annotations

import json
import subprocess
from typing import Any

from ..models import Connection, Kind, LinkState


def classify(name: str, link_type: str) -> tuple[Kind, bool]:
    n = name.lower()
    if link_type == "loopback" or n == "lo":
        return Kind.LOOPBACK, True
    if n.startswith(("wg", "tun", "tap", "tailscale", "ppp")):
        return Kind.VPN, True
    if n.startswith(("docker", "br-", "veth", "virbr", "vmnet", "cni", "flannel")):
        return Kind.VIRTUAL, True
    if n.startswith(("wl", "wlan")):
        return Kind.WIFI, False
    if n.startswith(("wwan", "usb", "rmnet")):
        return Kind.MOBILE, False
    if n.startswith(("en", "eth")):
        return Kind.ETHERNET, False
    return Kind.UNKNOWN, False


def parse(addrs: list[dict[str, Any]], routes: list[dict[str, Any]]) -> list[Connection]:
    gw: dict[str, tuple[str, int]] = {}
    for r in routes:
        if r.get("dst") != "default" or "gateway" not in r:
            continue
        dev, metric = r.get("dev"), int(r.get("metric", 0))
        if dev not in gw or metric < gw[dev][1]:
            gw[dev] = (r["gateway"], metric)
    out = []
    for a in addrs:
        name = a.get("ifname", "?")
        kind, virtual = classify(name, a.get("link_type", ""))
        flags = a.get("flags", [])
        oper = str(a.get("operstate", "")).upper()
        if "UP" not in flags:
            link = LinkState.DISABLED
        elif oper in ("UP", "UNKNOWN") and "NO-CARRIER" not in flags:
            link = LinkState.UP
        else:
            link = LinkState.DOWN
        ips = [i["local"] for i in a.get("addr_info", []) if i.get("family") == "inet" and not i["local"].startswith("169.254.")]
        g = gw.get(name)
        out.append(Connection(
            id=f"lnx-{a.get('ifindex')}", name=name, description=a.get("link_type", ""),
            kind=kind, link=link, if_index=a.get("ifindex"), mac=a.get("address"),
            ipv4=ips, gateway=g[0] if g else None, route_metric=g[1] if g else None,
            is_virtual=virtual, source="linux",
        ))
    return out


class LinuxDiscovery:
    name = "linux"

    def discover(self) -> list[Connection]:
        addrs = json.loads(subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, check=True, timeout=10).stdout)
        routes = json.loads(subprocess.run(["ip", "-j", "-4", "route"], capture_output=True, text=True, check=True, timeout=10).stdout or "[]")
        return parse(addrs, routes)
