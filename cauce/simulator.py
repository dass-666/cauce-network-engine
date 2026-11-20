"""Simulador de red con conexiones independientes.

IMPORTANTE: esto es una SIMULACIÓN. Ejercita la lógica del motor (salud,
leases, conmutación, rotación, auditoría, limitación) con conexiones
ficticias. No prueba hardware, controladores ni enrutamiento real de Windows.

Cada conexión simulada tiene su IP local, gateway e IP pública propios, y un
estado controlable: ``up``, ``down`` (enlace activo pero sin internet) o
``disconnected`` (sin enlace). El "servidor de laboratorio" simulado responde
con la IP pública de la conexión por la que llegó el tráfico, lo que permite
comprobar la ruta igual que con el servidor de laboratorio real.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

from .models import Connection, Kind, LinkState, ProbeResult
from .net import ConnectFailed, LabRequest, Response, ResponseLost


@dataclass
class SimLink:
    id: str
    name: str
    kind: Kind
    local_ip: str
    gateway: str
    public_ip: str
    metric: int
    state: str = "up"           # up | down | disconnected
    latency_ms: float = 20.0
    drop_responses: bool = False  # simula respuestas perdidas tras enviar


@dataclass
class TrafficRecord:
    connection_id: str
    method: str
    url: str
    status: Optional[int]
    outcome: str


ServiceHandler = Callable[[SimLink, LabRequest], tuple[int, dict, bytes]]


def default_service(link: SimLink, req: LabRequest) -> tuple[int, dict, bytes]:
    path = urlsplit(req.url).path
    if path.startswith("/limit"):
        return 429, {"retry-after": "60"}, b'{"error":"rate limited"}'
    return 200, {}, b'{"ok":true}'


class SimulatedNetwork:
    def __init__(self, links: list[SimLink], service: ServiceHandler = default_service):
        self.links = {l.id: l for l in links}
        self.service = service
        self.traffic: list[TrafficRecord] = []
        self.applied: dict[str, int] = {}   # efectos de operaciones (para detectar dobles ejecuciones)
        self._lock = threading.Lock()

    # --- control del escenario ----------------------------------------
    def set_state(self, conn_id: str, state: str) -> None:
        assert state in ("up", "down", "disconnected")
        self.links[conn_id].state = state

    # --- proveedor de descubrimiento ----------------------------------
    name = "simulated"

    def discover(self) -> list[Connection]:
        out = []
        for l in self.links.values():
            link = LinkState.DOWN if l.state == "disconnected" else LinkState.UP
            out.append(Connection(
                id=l.id, name=l.name, description=f"Conexión simulada ({l.kind.value})", kind=l.kind,
                link=link, ipv4=[l.local_ip] if link == LinkState.UP else [],
                gateway=l.gateway if link == LinkState.UP else None, route_metric=l.metric,
                public_ip=l.public_ip if link == LinkState.UP else None, source="simulated",
            ))
        return out

    # --- red ----------------------------------------------------------
    def _link_for(self, conn: Connection) -> SimLink:
        l = self.links.get(conn.id)
        if l is None or conn.primary_ip != l.local_ip:
            raise ConnectFailed("IP de origen no pertenece a ninguna conexión simulada")
        return l

    def probe(self, conn: Connection, target: str, timeout: float) -> ProbeResult:
        try:
            l = self._link_for(conn)
        except ConnectFailed as e:
            return ProbeResult(conn.id, target, False, None, str(e), None)
        if l.state != "up":
            return ProbeResult(conn.id, target, False, None, f"simulado: {l.state}", None)
        return ProbeResult(conn.id, target, True, l.latency_ms, None, l.local_ip)

    def request(self, conn: Connection, req: LabRequest) -> Response:
        l = self._link_for(conn)
        if l.state != "up":
            with self._lock:
                self.traffic.append(TrafficRecord(l.id, req.method, req.url, None, "connect_failed"))
            raise ConnectFailed(f"simulado: conexión {l.state}")
        status, headers, body = self.service(l, req)
        key = req.headers.get("Idempotency-Key") or f"{req.method} {req.url}"
        with self._lock:
            if req.method not in ("GET", "HEAD", "OPTIONS") and 200 <= status < 300:
                if req.headers.get("Idempotency-Key"):
                    self.applied.setdefault(key, 1)   # el servidor deduplica por clave
                else:
                    self.applied[key] = self.applied.get(key, 0) + 1
            self.traffic.append(TrafficRecord(l.id, req.method, req.url, status, "lost" if l.drop_responses else "ok"))
        if l.drop_responses:
            raise ResponseLost("simulado: respuesta perdida tras enviar")
        return Response(status, headers, body, local_ip=l.local_ip, observed_ip=l.public_ip, carried_by=l.id)


def three_links() -> list[SimLink]:
    """Las tres conexiones del escenario de prueba funcional."""
    return [
        SimLink("sim-a", "SIM Fibra (principal)", Kind.ETHERNET, "10.10.1.20", "10.10.1.1", "198.51.100.10", 10, "up", 12.0),
        SimLink("sim-b", "SIM LTE (alternativa)", Kind.MOBILE, "10.20.2.30", "10.20.2.1", "203.0.113.20", 50, "up", 45.0),
        SimLink("sim-c", "SIM Wi‑Fi (desconectada)", Kind.WIFI, "10.30.3.40", "10.30.3.1", "192.0.2.30", 30, "disconnected", 25.0),
    ]
