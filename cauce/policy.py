"""Rotación controlada de conexiones, SOLO para tareas de laboratorio propio.

Controles de seguridad (no configurables por debajo de los mínimos):
- Intervalo mínimo entre cambios: ``MIN_INTERVAL_S``.
- Máximo de cambios por hora: ``MAX_CHANGES_PER_HOUR``.
- Solo se aplica a tareas cuyos destinos están en la lista de laboratorio.
- Nunca se dispara por errores ni por respuestas de limitación (429/403):
  una limitación DETIENE la tarea (ver ``executor``) y además pausa la
  política, para que la rotación no pueda usarse para sortearla.
- Estados: active / paused / stopped. ``stopped`` es definitivo para esa
  instancia (hay que crear otra política).
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from .audit import AuditLog
from .routing import Lease, RouteManager

MIN_INTERVAL_S = 10.0
MAX_CHANGES_PER_HOUR = 60


class PolicyError(Exception):
    pass


@dataclass
class RotationPolicy:
    name: str
    pool: list[str]                     # ids de conexión, en orden
    interval_s: float = 60.0
    max_changes_per_hour: int = 20
    state: str = "paused"
    lease_ids: list[str] = field(default_factory=list)
    changes: deque = field(default_factory=deque)   # marcas de tiempo de cambios
    last_change: float = 0.0
    pause_reason: Optional[str] = None

    def validate(self) -> None:
        if len(self.pool) < 2:
            raise PolicyError("Una política de rotación necesita al menos 2 conexiones")
        if self.interval_s < MIN_INTERVAL_S:
            raise PolicyError(f"interval_s mínimo es {MIN_INTERVAL_S}s")
        if not (1 <= self.max_changes_per_hour <= MAX_CHANGES_PER_HOUR):
            raise PolicyError(f"max_changes_per_hour debe estar entre 1 y {MAX_CHANGES_PER_HOUR}")

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "name": self.name, "pool": self.pool, "interval_s": self.interval_s,
            "max_changes_per_hour": self.max_changes_per_hour, "state": self.state,
            "lease_ids": list(self.lease_ids),
            "changes_last_hour": sum(1 for t in self.changes if now - t < 3600),
            "last_change": self.last_change or None, "pause_reason": self.pause_reason,
        }


class RotationController:
    def __init__(self, routes: RouteManager, audit: AuditLog, clock: Callable[[], float] = time.time):
        self.routes = routes
        self.audit = audit
        self.clock = clock
        self._policies: dict[str, RotationPolicy] = {}
        self._lock = threading.RLock()

    def create(self, name: str, pool: list[str], interval_s: float = 60.0, max_changes_per_hour: int = 20) -> RotationPolicy:
        p = RotationPolicy(name=name, pool=list(pool), interval_s=float(interval_s),
                           max_changes_per_hour=int(max_changes_per_hour))
        p.validate()
        with self._lock:
            if name in self._policies and self._policies[name].state != "stopped":
                raise PolicyError(f"Ya existe la política {name}")
            self._policies[name] = p
        self.audit.record("rotation.created", f"Política '{name}' creada (pausada)", **p.to_dict())
        return p

    def get(self, name: str) -> RotationPolicy:
        with self._lock:
            p = self._policies.get(name)
        if not p:
            raise PolicyError(f"No existe la política {name}")
        return p

    def all(self) -> list[RotationPolicy]:
        with self._lock:
            return list(self._policies.values())

    def attach(self, name: str, lease: Lease) -> None:
        p = self.get(name)
        if lease.connection_id not in p.pool:
            raise PolicyError("La conexión del lease no pertenece al pool de la política")
        with self._lock:
            p.lease_ids.append(lease.id)

    def detach(self, lease_id: str) -> None:
        with self._lock:
            for p in self._policies.values():
                if lease_id in p.lease_ids:
                    p.lease_ids.remove(lease_id)

    def set_state(self, name: str, state: str, reason: str = "operador") -> RotationPolicy:
        if state not in ("active", "paused", "stopped"):
            raise PolicyError("Estado inválido")
        p = self.get(name)
        with self._lock:
            if p.state == "stopped":
                raise PolicyError("La política está detenida definitivamente")
            old, p.state = p.state, state
            p.pause_reason = reason if state != "active" else None
        self.audit.record("rotation.state", f"Política '{name}': {old} -> {state} ({reason})",
                          policy=name, old=old, new=state, reason=reason)
        return p

    def pause_for_lease(self, lease_id: str, reason: str) -> None:
        for p in self.all():
            if lease_id in p.lease_ids and p.state == "active":
                self.set_state(p.name, "paused", reason)

    def stop_all(self, reason: str) -> None:
        for p in self.all():
            if p.state != "stopped":
                self.set_state(p.name, "stopped", reason)

    def tick(self) -> list[dict]:
        """Aplica como máximo un cambio por política si toca. Devuelve los cambios hechos."""
        now = self.clock()
        done = []
        for p in self.all():
            with self._lock:
                if p.state != "active":
                    continue
                while p.changes and now - p.changes[0] >= 3600:
                    p.changes.popleft()
                if now - p.last_change < p.interval_s:
                    continue
                if len(p.changes) >= p.max_changes_per_hour:
                    p.state, p.pause_reason = "paused", "límite de cambios por hora alcanzado"
                    self.audit.record("rotation.limit", f"Política '{p.name}' pausada: límite por hora", policy=p.name)
                    continue
                healthy = [c for c in p.pool if self.routes.is_available(c)]
                if len(healthy) < 2:
                    continue  # no se rota a una conexión caída ni se "rota" a la misma
                lease_ids = list(p.lease_ids)
            changed = False
            for lid in lease_ids:
                lease = self.routes.get(lid)
                if not lease or lease.released:
                    self.detach(lid)
                    continue
                cur = lease.connection_id
                order = [c for c in p.pool if c in healthy]
                nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
                if nxt != cur:
                    self.routes.reassign(lease, nxt, f"rotación programada '{p.name}'", "rotation")
                    lease.preferred_connection_id = nxt  # la rotación redefine la preferida
                    changed = True
                    done.append({"policy": p.name, "lease_id": lid, "from": cur, "to": nxt})
            if changed:
                with self._lock:
                    p.last_change = now
                    p.changes.append(now)
        return done
