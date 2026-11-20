"""Análisis de independencia de salidas.

Dos interfaces NO implican dos salidas a internet. Ejemplos frecuentes:
- Ethernet y Wi‑Fi conectados al mismo router: mismo gateway, misma IP pública.
- Un adaptador virtual (Hyper-V, WSL) que reenvía por la tarjeta física.
- Una VPN: tiene IP pública propia, pero viaja por otra interfaz física.

Reglas (de más a menos evidencia):
1. Misma IP pública observada -> salida compartida.
2. Mismo gateway (misma IP y, si se conoce, misma MAC) -> salida compartida.
3. Gateway distinto + IP pública distinta verificada -> independiente confirmada.
4. Gateway distinto sin verificación de IP pública -> probablemente independiente.
5. Virtual/VPN -> se marca compartida con su interfaz portadora si se conoce;
   si no, desconocida.

Incluso "confirmada" solo significa salidas distintas observadas; dos ISP
pueden compartir infraestructura aguas arriba. Eso no es detectable desde el
equipo y se documenta como limitación.
"""
from __future__ import annotations

from .models import Connection, Independence, Kind


def analyze(conns: list[Connection]) -> list[Connection]:
    candidates = [c for c in conns if c.usable and c.kind not in (Kind.LOOPBACK,)]
    groups: dict[str, list[Connection]] = {}
    for c in candidates:
        if c.public_ip:
            key = f"pub:{c.public_ip}"
        elif c.gateway:
            key = f"gw:{c.gateway}"
        else:
            key = f"solo:{c.id}"
        groups.setdefault(key, []).append(c)

    # Si dos grupos por gateway acaban con la misma IP pública, ya se fusionaron arriba.
    for key, members in groups.items():
        for c in members:
            c.egress_group = key
            if c.kind in (Kind.VPN, Kind.VIRTUAL):
                c.independence = Independence.SHARED if len(members) > 1 else Independence.UNKNOWN
            elif len(members) > 1:
                c.independence = Independence.SHARED
            elif not c.gateway:
                c.independence = Independence.UNKNOWN
            elif c.public_ip:
                c.independence = Independence.CONFIRMED
            else:
                c.independence = Independence.LIKELY
    for c in conns:
        if c not in candidates:
            c.egress_group = None
            c.independence = Independence.UNKNOWN
    return conns
