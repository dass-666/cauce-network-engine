"""Observador de MikroTik RouterOS v7 por REST (solo lectura).

Lee tres recursos (``GET /rest/interface``, ``/rest/ip/route`` y ``/rest/tool/netwatch``) y deduce el
estado de cada WAN. Convención que debe seguir quien configure el router: cada WAN se identifica por el
**comentario** de su ruta por defecto y de su comprobación de netwatch (p. ej. ``WAN1``, ``WAN2``).

RouterOS devuelve booleanos y números como texto ("true", "12ms"); aquí se convierten con cuidado y un
valor ilegible queda como desconocido, nunca como «caído».
FORMATO NO VERIFICADO CON UN EQUIPO REAL: las pruebas usan respuestas simuladas.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .contract import OFFLINE, ONLINE, UNKNOWN, RestReadTransport, RouterSnapshot, RouterUnavailable, WanStatus, summarize

_KINDS = {"ether": "ethernet", "lte": "lte", "wlan": "wifi", "wifi": "wifi", "pppoe-out": "ethernet"}
_MS = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(us|ms|s)?\s*$")


def _bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v in ("true", "yes"):
        return True
    if v in ("false", "no"):
        return False
    return None


def _ms(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = _MS.match(str(v)) if v is not None else None
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2) or "ms"
    return round(n / 1000 if unit == "us" else n * 1000 if unit == "s" else n, 2)


def _pct(v: Any) -> Optional[float]:
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None


class MikroTikObserver:
    vendor = "mikrotik"

    def __init__(self, name: str, transport: RestReadTransport):
        self.name = name
        self.transport = transport

    def observe(self) -> RouterSnapshot:
        try:
            ifaces = self.transport.get("interface")
            routes = self.transport.get("ip/route")
            watches = self.transport.get("tool/netwatch")
        except RouterUnavailable:
            return RouterSnapshot(self.name, self.vendor, "unknown", "router_unreachable")
        if not all(isinstance(x, list) for x in (ifaces, routes, watches)):
            return RouterSnapshot(self.name, self.vendor, "unknown", "invalid_response")
        by_name = {i.get("name"): i for i in ifaces if isinstance(i, dict)}
        wans: list[WanStatus] = []
        for r in routes:
            if not isinstance(r, dict) or r.get("dst-address") != "0.0.0.0/0" or not r.get("comment"):
                continue
            label = str(r["comment"])[:32]
            gw = str(r.get("immediate-gw") or "")      # p. ej. "192.168.1.1%ether1": la interfaz va tras «%»
            iface = by_name.get(gw.split("%", 1)[1]) if "%" in gw else None
            w = WanStatus(label=label, active=_bool(r.get("active")))
            if iface:
                w.kind = _KINDS.get(str(iface.get("type")), "unknown")
                running, disabled = _bool(iface.get("running")), _bool(iface.get("disabled"))
                w.link_up = None if running is None else (running and not disabled)
            nw = next((n for n in watches if isinstance(n, dict) and n.get("comment") == r["comment"]), None)
            if nw:
                st = nw.get("status")
                w.internet = ONLINE if st == "up" else OFFLINE if st == "down" else UNKNOWN
                w.latency_ms = _ms(nw.get("rtt-avg"))
                w.loss_pct = _pct(nw.get("loss-percent"))
            elif w.link_up is False:
                w.internet = OFFLINE
            wans.append(w)
        return summarize(self.name, self.vendor, wans)
