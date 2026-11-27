"""Observador de OpenWrt con mwan3 por ubus (solo lectura).

Lee ``ubus call mwan3 status`` (estado de cada interfaz WAN según el seguimiento de mwan3) y
``ubus call network.interface dump`` (enlace y si lleva la ruta por defecto). Por HTTP esto se haría con
JSON-RPC de uhttpd-mod-ubus/rpcd y un usuario con ACL de solo lectura (fase posterior).
FORMATO NO VERIFICADO CON UN EQUIPO REAL: las pruebas usan respuestas simuladas.
"""
from __future__ import annotations

from typing import Any, Optional

from .contract import OFFLINE, ONLINE, UNKNOWN, RouterSnapshot, RouterUnavailable, UbusReadTransport, WanStatus, summarize

_STATUS = {"online": ONLINE, "offline": OFFLINE, "disconnecting": OFFLINE, "connecting": UNKNOWN,
           "disabled": OFFLINE, "unknown": UNKNOWN}
_PROTO_KIND = {"dhcp": "ethernet", "pppoe": "ethernet", "static": "ethernet", "qmi": "lte", "mbim": "lte",
               "ncm": "lte", "modemmanager": "lte", "3g": "lte"}


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class OpenWrtObserver:
    vendor = "openwrt"

    def __init__(self, name: str, transport: UbusReadTransport):
        self.name = name
        self.transport = transport

    def observe(self) -> RouterSnapshot:
        try:
            mw = self.transport.call("mwan3", "status")
            dump = self.transport.call("network.interface", "dump")
        except RouterUnavailable:
            return RouterSnapshot(self.name, self.vendor, "unknown", "router_unreachable")
        ifs = mw.get("interfaces") if isinstance(mw, dict) else None
        lst = dump.get("interface") if isinstance(dump, dict) else None
        if not isinstance(ifs, dict) or not isinstance(lst, list):
            return RouterSnapshot(self.name, self.vendor, "unknown", "invalid_response")
        net = {i.get("interface"): i for i in lst if isinstance(i, dict)}
        wans = []
        for label, st in sorted(ifs.items()):
            if not isinstance(st, dict):
                continue
            n = net.get(label, {})
            w = WanStatus(label=str(label)[:32], kind=_PROTO_KIND.get(str(n.get("proto")), "unknown"))
            w.link_up = n.get("up") if isinstance(n.get("up"), bool) else None
            w.internet = _STATUS.get(str(st.get("status")), UNKNOWN)
            routes = n.get("route") if isinstance(n.get("route"), list) else []
            has_default = any(isinstance(r, dict) and r.get("target") == "0.0.0.0" and r.get("mask") == 0 for r in routes)
            w.active = bool(has_default and w.internet == ONLINE) if n else None
            track = [t for t in st.get("track_ip") or [] if isinstance(t, dict)]
            lat = [_num(t.get("latency")) for t in track if _num(t.get("latency")) is not None]
            loss = [_num(t.get("packetloss")) for t in track if _num(t.get("packetloss")) is not None]
            w.latency_ms = round(sum(lat) / len(lat), 2) if lat else None
            w.loss_pct = round(sum(loss) / len(loss), 1) if loss else None
            wans.append(w)
        # mwan3 puede tener varias WAN con ruta por defecto (balanceo); «activa» solo si hay una
        return summarize(self.name, self.vendor, wans)
