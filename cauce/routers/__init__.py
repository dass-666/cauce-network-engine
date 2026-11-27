"""Observación de routers externos (solo lectura). Ver contract.py para las reglas.

Configuración (``routers`` en config.json), en esta versión solo con transporte simulado::

    "routers": [{"name": "router-lab", "vendor": "mikrotik", "transport": "simulated", "scenario": "normal",
                 "interval_s": 10}]
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

from .contract import (ReadOnlyViolation, RouterSnapshot, RouterUnavailable,  # noqa: F401
                       WanStatus, summarize)
from .mikrotik import MikroTikObserver
from .openwrt import OpenWrtObserver
from .simulated import SimulatedRest, SimulatedUbus, scenarios

ADAPTERS = {"mikrotik": (MikroTikObserver, SimulatedRest), "openwrt": (OpenWrtObserver, SimulatedUbus)}
TRANSPORTS = ("simulated",)
MIN_INTERVAL_S, DEFAULT_INTERVAL_S = 5.0, 10.0
_ALLOWED_KEYS = {"name", "vendor", "transport", "scenario", "interval_s"}


class RouterConfigError(ValueError):
    pass


def validate_router_config(entries: list[dict]) -> None:
    from ..config import ConfigError
    if not isinstance(entries, list) or len(entries) > 4:
        raise ConfigError("routers debe ser una lista de como mucho 4 routers")
    names = set()
    for e in entries:
        if not isinstance(e, dict):
            raise ConfigError("cada router debe ser un objeto")
        extra = set(e) - _ALLOWED_KEYS
        if extra:
            # Cualquier campo extra (host, usuario, contraseña…) se rechaza: en esta fase no hay transporte real.
            raise ConfigError(f"campos no admitidos en routers: {', '.join(sorted(extra))}")
        if e.get("vendor") not in ADAPTERS:
            raise ConfigError(f"vendor de router desconocido: {e.get('vendor')!r} (admitidos: {', '.join(ADAPTERS)})")
        if e.get("transport", "simulated") not in TRANSPORTS:
            raise ConfigError("solo hay transportes simulados en esta versión; un router real necesita una fase posterior")
        if e.get("scenario", "normal") not in scenarios(e["vendor"]):
            raise ConfigError(f"escenario simulado desconocido: {e.get('scenario')!r}")
        name = str(e.get("name") or "")
        if not name or len(name) > 32 or name in names:
            raise ConfigError("cada router necesita un name único de 1 a 32 caracteres")
        names.add(name)
        if float(e.get("interval_s", DEFAULT_INTERVAL_S)) < MIN_INTERVAL_S:
            raise ConfigError(f"interval_s de un router no puede ser menor que {MIN_INTERVAL_S}")


class RouterWatch:
    """Guarda la última observación de un router y anota en la auditoría cada cambio de estado o de WAN activa."""

    def __init__(self, observer: Any, audit: Any, interval_s: float = DEFAULT_INTERVAL_S,
                 clock=time.time):
        self.observer = observer
        self.audit = audit
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s))
        self.clock = clock
        self.last: Optional[RouterSnapshot] = None
        self._next = 0.0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.observer.name

    def poll(self, force: bool = False) -> Optional[RouterSnapshot]:
        now = self.clock()
        with self._lock:
            if not force and now < self._next:
                return self.last
            self._next = now + self.interval_s
        try:
            snap = self.observer.observe()
        except ReadOnlyViolation:
            raise                                           # defecto de programación: que la prueba lo vea
        except Exception as e:                              # noqa: BLE001  un router raro nunca tumba el motor
            snap = RouterSnapshot(self.observer.name, self.observer.vendor, "unknown", "observer_error")
            self.audit.record("router.error", f"Fallo al observar {self.observer.name}: {type(e).__name__}",
                              router=self.observer.name)
        prev = self.last
        self.last = snap
        if prev is None or (prev.state, prev.active_wan) != (snap.state, snap.active_wan):
            self.audit.record("router.state", f"Router {snap.router}: {prev.state if prev else '-'} -> {snap.state}, "
                              f"WAN activa {prev.active_wan if prev else '-'} -> {snap.active_wan}",
                              router=snap.router, old=prev.state if prev else None, new=snap.state,
                              active_wan=snap.active_wan, reason=snap.reason)
        return snap


def build_observers(entries: list[dict], audit: Any) -> list[RouterWatch]:
    out = []
    for e in entries:
        observer_cls, transport_cls = ADAPTERS[e["vendor"]]
        transport = transport_cls(e.get("scenario", "normal"))
        out.append(RouterWatch(observer_cls(e["name"], transport), audit, e.get("interval_s", DEFAULT_INTERVAL_S)))
    return out
