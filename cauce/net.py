"""Capa de transporte: cómo el motor envía tráfico POR una conexión concreta.

Mecanismo real: enlazar (bind) el socket a la IP local de la interfaz elegida
antes de conectar. Así cada operación elige su salida sin tocar la tabla de
rutas global del sistema y sin afectar a otras aplicaciones.

- Windows (modelo de host fuerte para envío, por defecto desde Vista): el
  sistema solo usa rutas de la interfaz que posee la IP de origen. Para que
  funcione, esa interfaz necesita su propia ruta por defecto (lo normal si
  tiene gateway). PENDIENTE DE VERIFICAR EN HARDWARE REAL.
- Linux (modelo de host débil): el bind a la IP de origen NO garantiza la
  interfaz de salida sin reglas de enrutamiento por origen (``ip rule``).
  El motor lo detecta comprobando la IP local usada y la IP que ve el
  servidor de laboratorio.

La verificación se hace siempre después del envío: ``Response.local_ip`` es
la IP con la que salió el socket, y ``observed_ip`` es la que vio el servidor
de laboratorio (cabecera ``X-Cauce-Observed-Ip``).
"""
from __future__ import annotations

import http.client
import socket
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol
from urllib.parse import urlsplit

from .models import Connection, ProbeResult


class ConnectFailed(Exception):
    """No se pudo establecer la conexión: la petición NO llegó a enviarse."""


class ResponseLost(Exception):
    """La petición pudo haberse enviado, pero no hubo respuesta completa."""


@dataclass
class LabRequest:
    method: str
    url: str
    body: Optional[bytes] = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 10.0


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    local_ip: Optional[str]          # IP de origen real del socket
    observed_ip: Optional[str]       # IP que vio el servidor (si lo informa)
    carried_by: Optional[str] = None  # solo simulador: id de conexión que transportó el tráfico


class Network(Protocol):
    def probe(self, conn: Connection, target: str, timeout: float) -> ProbeResult: ...
    def request(self, conn: Connection, req: LabRequest) -> Response: ...


def _split_target(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host, int(port)


class RealNetwork:
    """Transporte real con sockets enlazados a la IP de origen."""

    def probe(self, conn: Connection, target: str, timeout: float) -> ProbeResult:
        ip = conn.primary_ip
        if not ip:
            return ProbeResult(conn.id, target, False, None, "sin IPv4", None)
        host, port = _split_target(target)
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout, source_address=(ip, 0)) as s:
                local = s.getsockname()[0]
            ms = (time.perf_counter() - t0) * 1000
            if local != ip:
                return ProbeResult(conn.id, target, False, ms, f"salió por {local}, no por {ip}", local)
            return ProbeResult(conn.id, target, True, round(ms, 2), None, local)
        except OSError as e:
            return ProbeResult(conn.id, target, False, None, f"{type(e).__name__}: {e}", None)

    def request(self, conn: Connection, req: LabRequest) -> Response:
        ip = conn.primary_ip
        if not ip:
            raise ConnectFailed("la conexión no tiene IPv4")
        u = urlsplit(req.url)
        if u.scheme not in ("http", "https"):
            raise ConnectFailed(f"esquema no soportado: {u.scheme}")
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        port = u.port or (443 if u.scheme == "https" else 80)
        hc = cls(u.hostname, port, timeout=req.timeout, source_address=(ip, 0))
        try:
            try:
                hc.connect()
            except OSError as e:
                raise ConnectFailed(f"{type(e).__name__}: {e}") from e
            local = hc.sock.getsockname()[0] if hc.sock else None
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            try:
                hc.request(req.method, path, body=req.body, headers=req.headers)
                r = hc.getresponse()
                body = r.read()
            except OSError as e:
                raise ResponseLost(f"{type(e).__name__}: {e}") from e
            except http.client.HTTPException as e:
                raise ResponseLost(f"{type(e).__name__}: {e}") from e
            headers = {k.lower(): v for k, v in r.getheaders()}
            return Response(r.status, headers, body, local, headers.get("x-cauce-observed-ip"))
        finally:
            hc.close()
