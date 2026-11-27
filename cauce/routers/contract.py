"""Contrato de adaptadores de OBSERVACIÓN de routers (solo lectura).

Objetivo: que Network Engine pueda informar qué ve un router externo (qué WAN está activa, si cada una
tiene internet, su latencia) sin que el motor ni sus consumidores dependan de un fabricante concreto.

Reglas del contrato (las comprueban las pruebas en tests/test_routers.py):

1. **Solo lectura por construcción.** Un adaptador nunca recibe un cliente HTTP genérico: recibe un
   *transporte* que solo sabe leer. ``RestReadTransport.get(path)`` (estilo MikroTik) y
   ``UbusReadTransport.call(obj, method)`` (estilo OpenWrt, limitado a una lista blanca de llamadas de
   lectura). No existe ningún método para escribir, y un adaptador que intente usar otro lo rechaza el
   propio transporte (``ReadOnlyViolation``).
2. **Sin datos sensibles.** La vista que sale del adaptador no lleva IP públicas, MAC, gateways ni
   credenciales: solo etiqueta de la WAN, tipo, estados, latencia y pérdida.
3. **«No sé» no es «caído».** Si el router no responde o la respuesta es rara, el estado es ``unknown``,
   nunca ``offline``.
4. **El adaptador no decide nada.** No conmuta ni cambia la WAN del router: eso lo hace el router con su
   propia configuración de failover. Cauce solo observa y lo anota en la auditoría.
5. En esta versión **solo existen transportes simulados** (``SimulatedRest`` y ``SimulatedUbus``). Un
   transporte real (HTTPS con usuario de solo lectura guardado con DPAPI) es trabajo de una fase posterior
   y necesita hardware para validarse.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol


class ReadOnlyViolation(Exception):
    """Un adaptador intentó una operación que no es de lectura."""


class RouterUnavailable(Exception):
    """El router no respondió o la respuesta no se pudo interpretar."""


# Estados de una WAN vistos por el router
ONLINE, OFFLINE, UNKNOWN = "online", "offline", "unknown"


@dataclass
class WanStatus:
    label: str                       # nombre/comentario de la WAN en el router (p. ej. "WAN1", "wanb")
    kind: str = "unknown"            # ethernet | lte | wifi | unknown
    link_up: Optional[bool] = None   # enlace físico/lógico levantado
    internet: str = UNKNOWN          # online | offline | unknown (según la comprobación del propio router)
    active: Optional[bool] = None    # True si es la que lleva ahora la ruta por defecto
    latency_ms: Optional[float] = None
    loss_pct: Optional[float] = None


@dataclass
class RouterSnapshot:
    router: str                      # nombre local que le da el operador en la configuración
    vendor: str
    state: str                       # usable | degraded | unavailable | unknown
    reason: Optional[str]
    observed_at: float = field(default_factory=time.time)
    wans: list[WanStatus] = field(default_factory=list)

    @property
    def active_wan(self) -> Optional[str]:
        act = [w.label for w in self.wans if w.active]
        return act[0] if len(act) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["active_wan"] = self.active_wan
        return d


def summarize(router: str, vendor: str, wans: list[WanStatus]) -> RouterSnapshot:
    """Estado global a partir de las WAN, con la misma semántica que /status/connectivity."""
    if not wans:
        return RouterSnapshot(router, vendor, "unknown", "no_wans_reported", wans=wans)
    online = [w for w in wans if w.internet == ONLINE]
    if online:
        degraded = all((w.loss_pct or 0) >= 20 for w in online)
        return RouterSnapshot(router, vendor, "degraded" if degraded else "usable",
                              "high_loss" if degraded else None, wans=wans)
    if all(w.internet == OFFLINE for w in wans):
        return RouterSnapshot(router, vendor, "unavailable", "all_wans_offline", wans=wans)
    return RouterSnapshot(router, vendor, "unknown", "insufficient_information", wans=wans)


class RestReadTransport(Protocol):
    """Transporte REST de solo lectura: equivale a ``GET https://<router>/rest/<path>``."""

    def get(self, path: str) -> Any: ...


class UbusReadTransport(Protocol):
    """Transporte ubus de solo lectura: equivale a ``ubus call <obj> <method>`` vía JSON-RPC."""

    def call(self, obj: str, method: str) -> Any: ...


class RouterObserver(Protocol):
    vendor: str
    name: str

    def observe(self) -> RouterSnapshot: ...
