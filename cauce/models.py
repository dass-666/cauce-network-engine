"""Modelos de datos compartidos por todos los módulos.

Se usan dataclasses de la biblioteca estándar para no depender de terceros.
Ningún modelo contiene contraseñas ni credenciales: el motor no las necesita
para gestionar rutas, y si en el futuro se necesitan se guardan cifradas
(ver ``cauce.secrets``).
"""
from __future__ import annotations

import enum
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class Kind(str, enum.Enum):
    ETHERNET = "ethernet"
    WIFI = "wifi"
    MOBILE = "mobile"          # banda ancha móvil (WWAN) o módem USB
    TETHER = "tether"          # teléfono compartido por USB/Bluetooth (RNDIS)
    VPN = "vpn"
    VIRTUAL = "virtual"        # Hyper-V, VirtualBox, WSL, Docker...
    LOOPBACK = "loopback"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


class LinkState(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


class Health(str, enum.Enum):
    """Estado de salud calculado por el monitor (no el estado del enlace)."""
    UNKNOWN = "unknown"      # aún sin muestras suficientes
    HEALTHY = "healthy"
    DEGRADED = "degraded"    # responde, pero con fallos o latencia alta
    DOWN = "down"            # fallos consecutivos por encima del umbral


class Independence(str, enum.Enum):
    """Qué tan seguros estamos de que una conexión tiene salida propia."""
    CONFIRMED = "confirmed_independent"   # gateway distinto e IP pública distinta observada
    LIKELY = "likely_independent"         # gateway distinto, IP pública no verificada
    SHARED = "shared_egress"              # comparte gateway o IP pública con otra
    UNKNOWN = "unknown"


@dataclass
class Connection:
    id: str
    name: str
    description: str
    kind: Kind
    link: LinkState
    if_index: Optional[int] = None
    mac: Optional[str] = None
    ipv4: list[str] = field(default_factory=list)
    gateway: Optional[str] = None
    route_metric: Optional[int] = None
    is_virtual: bool = False
    public_ip: Optional[str] = None        # solo si se comprobó con un destino autorizado
    independence: Independence = Independence.UNKNOWN
    egress_group: Optional[str] = None     # conexiones con el mismo grupo comparten salida
    source: str = "unknown"                # windows | linux | simulated

    @property
    def primary_ip(self) -> Optional[str]:
        return self.ipv4[0] if self.ipv4 else None

    @property
    def usable(self) -> bool:
        return (
            self.link == LinkState.UP
            and bool(self.ipv4)
            and self.kind not in (Kind.LOOPBACK,)
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["link"] = self.link.value
        d["independence"] = self.independence.value
        d["primary_ip"] = self.primary_ip
        d["usable"] = self.usable
        return d


@dataclass
class ProbeResult:
    connection_id: str
    target: str
    ok: bool
    latency_ms: Optional[float]
    error: Optional[str]
    local_ip_used: Optional[str]
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskState(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"            # detenida por el operador
    THROTTLED = "throttled"        # el servicio pidió limitar (429/503 Retry-After): se detiene
    FAILED = "failed"              # sin rutas disponibles o error no recuperable
    NEEDS_REVIEW = "needs_review"  # operación no idempotente con resultado incierto


TERMINAL_TASK_STATES = {
    TaskState.COMPLETED, TaskState.STOPPED, TaskState.THROTTLED,
    TaskState.FAILED, TaskState.NEEDS_REVIEW,
}
