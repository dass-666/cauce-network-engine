"""Resolución de rutas mediante "arrendamientos" (leases).

Cada operación obtiene su propio lease: un registro que dice "esta operación
sale por esta conexión". El tráfico se enlaza a la IP de origen del lease, de
modo que:

- No se modifica la tabla de rutas global de Windows.
- Dos operaciones no pueden cambiarse la ruta entre sí: un lease solo lo
  modifica el motor (conmutación/rotación de ESE lease) y solo lo libera su
  propietario, que debe presentar el ``owner`` con el que lo creó.
- Una petición explícita de conexión no se sustituye en silencio: si la
  conexión pedida no está sana, la petición falla con un motivo claro.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .audit import AuditLog
from .models import Connection, Health


class RouteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Lease:
    id: str
    owner: str
    purpose: str
    connection_id: str
    local_ip: Optional[str]
    explicit: bool                 # el solicitante pidió esta conexión concreta
    allow_failover: bool
    allow_failback: bool
    preferred_connection_id: str   # a la que se vuelve si se recupera
    created: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)
    released: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "owner": self.owner, "purpose": self.purpose,
                "connection_id": self.connection_id, "local_ip": self.local_ip,
                "explicit": self.explicit, "allow_failover": self.allow_failover,
                "allow_failback": self.allow_failback,
                "preferred_connection_id": self.preferred_connection_id,
                "created": self.created, "released": self.released,
                "history": list(self.history),
            }


_USABLE_HEALTH = (Health.HEALTHY, Health.DEGRADED)


class RouteManager:
    def __init__(self, audit: AuditLog, get_connections: Callable[[], list[Connection]],
                 health_of: Callable[[str], Health]):
        self.audit = audit
        self.get_connections = get_connections
        self.health_of = health_of
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()
        self.operator_choice: Optional[str] = None

    # --- utilidades -----------------------------------------------------
    def _conn(self, conn_id: str) -> Optional[Connection]:
        return next((c for c in self.get_connections() if c.id == conn_id), None)

    def is_available(self, conn_id: str) -> bool:
        c = self._conn(conn_id)
        return bool(c and c.usable and self.health_of(conn_id) in _USABLE_HEALTH)

    def candidates(self, exclude: Optional[str] = None) -> list[Connection]:
        """Conexiones utilizables, priorizando las que NO comparten salida con ``exclude``."""
        excl = self._conn(exclude) if exclude else None
        cs = [c for c in self.get_connections() if c.id != exclude and self.is_available(c.id)]

        def key(c: Connection):
            same_group = 1 if (excl and c.egress_group and c.egress_group == excl.egress_group) else 0
            degraded = 1 if self.health_of(c.id) == Health.DEGRADED else 0
            return (same_group, degraded, c.route_metric if c.route_metric is not None else 9999, c.id)
        return sorted(cs, key=key)

    # --- selección del operador -----------------------------------------
    def select(self, conn_id: str, actor: str = "operator") -> None:
        c = self._conn(conn_id)
        if not c:
            raise RouteError("unknown_connection", f"No existe la conexión {conn_id}")
        if not self.is_available(conn_id):
            raise RouteError("connection_unavailable", f"La conexión {c.name} no está sana ({self.health_of(conn_id).value})")
        old = self.operator_choice
        self.operator_choice = conn_id
        self.audit.record("route.selected", f"{actor} seleccionó {c.name} como ruta preferida",
                          connection_id=conn_id, previous=old, actor=actor)

    # --- leases ---------------------------------------------------------
    def acquire(self, owner: str, purpose: str, connection_id: Optional[str] = None,
                allow_failover: bool = True, allow_failback: bool = True) -> Lease:
        if not owner:
            raise RouteError("invalid_owner", "Se requiere un identificador de propietario")
        explicit = connection_id is not None
        if explicit:
            c = self._conn(connection_id)
            if not c:
                raise RouteError("unknown_connection", f"No existe la conexión {connection_id}")
            if not self.is_available(connection_id):
                raise RouteError("connection_unavailable",
                                 f"La conexión pedida {c.name} no está disponible ({self.health_of(connection_id).value}); no se sustituye en silencio")
        else:
            if self.operator_choice and self.is_available(self.operator_choice):
                c = self._conn(self.operator_choice)
            else:
                cands = self.candidates()
                if not cands:
                    raise RouteError("no_route", "No hay ninguna conexión sana disponible")
                c = cands[0]
        lease = Lease(
            id="ls_" + secrets.token_hex(8), owner=owner, purpose=purpose,
            connection_id=c.id, local_ip=c.primary_ip, explicit=explicit,
            allow_failover=allow_failover, allow_failback=allow_failback,
            preferred_connection_id=c.id,
        )
        lease.history.append({"ts": time.time(), "to": c.id, "reason": "acquire"})
        with self._lock:
            self._leases[lease.id] = lease
        self.audit.record("route.lease", f"Ruta {c.name} asignada a '{purpose}'",
                          lease_id=lease.id, owner=owner, connection_id=c.id, local_ip=c.primary_ip, explicit=explicit)
        return lease

    def get(self, lease_id: str) -> Optional[Lease]:
        with self._lock:
            return self._leases.get(lease_id)

    def leases(self, active_only: bool = True) -> list[Lease]:
        with self._lock:
            return [l for l in self._leases.values() if not (active_only and l.released)]

    def release(self, lease_id: str, owner: str) -> None:
        lease = self.get(lease_id)
        if not lease:
            raise RouteError("unknown_lease", "Lease inexistente")
        if lease.owner != owner:
            # Protección clave: nadie libera ni cambia la ruta de otra operación
            raise RouteError("not_owner", "El lease pertenece a otro propietario")
        with lease.lock:
            if lease.released:
                return
            lease.released = True
        self.audit.record("route.release", f"Ruta liberada ({lease.purpose})", lease_id=lease_id, owner=owner)

    def reassign(self, lease: Lease, new_conn_id: str, reason: str, kind: str) -> None:
        """Cambia la conexión de UN lease. Solo lo invoca el motor."""
        c = self._conn(new_conn_id)
        if not c:
            raise RouteError("unknown_connection", new_conn_id)
        with lease.lock:
            if lease.released:
                return
            old = lease.connection_id
            if old == new_conn_id:
                return
            lease.connection_id = c.id
            lease.local_ip = c.primary_ip
            lease.history.append({"ts": time.time(), "from": old, "to": c.id, "reason": reason, "kind": kind})
        self.audit.record(f"route.{kind}", f"'{lease.purpose}': {old} -> {c.id} ({reason})",
                          lease_id=lease.id, owner=lease.owner, from_connection=old, to_connection=c.id, reason=reason)

    def failover(self, lease: Lease, reason: str) -> Optional[str]:
        """Busca alternativa para un lease. Devuelve la nueva conexión o None."""
        if not lease.allow_failover:
            self.audit.record("route.failover_denied", f"'{lease.purpose}' no permite conmutación", lease_id=lease.id)
            return None
        cands = self.candidates(exclude=lease.connection_id)
        if not cands:
            self.audit.record("route.no_alternative", f"Sin alternativa para '{lease.purpose}'", lease_id=lease.id, reason=reason)
            return None
        self.reassign(lease, cands[0].id, reason, "failover")
        return cands[0].id

    def failback_if_possible(self, lease: Lease) -> bool:
        if (lease.allow_failback and not lease.released and lease.connection_id != lease.preferred_connection_id
                and self.is_available(lease.preferred_connection_id)):
            self.reassign(lease, lease.preferred_connection_id, "la conexión preferida se recuperó", "failback")
            return True
        return False
