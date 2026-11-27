"""Cliente Python mínimo para la API local de Cauce (solo biblioteca estándar).

Pensado para que una aplicación externa lo copie o lo instale sin depender de
nada más del motor. Ejemplo:

    from cauce.client import CauceClient
    c = CauceClient.from_local_install()          # lee el token de sesión
    print(c.connections())
    route = c.request_route(owner="mi-app", purpose="sincronizar")
    sock = c.connect_via(route, ("127.0.0.1", 9000))  # socket enlazado a esa ruta
    ...
    c.release_route(route["id"], owner="mi-app")
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import quote

# Copia literal de cauce.DEFAULT_API_PORT: este archivo debe poder copiarse a otro proyecto sin el paquete.
# Una prueba comprueba que ambos coinciden.
DEFAULT_API_PORT = 8780


class CauceAPIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class CauceClient:
    API = "/api/v1"

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = DEFAULT_API_PORT, timeout: float = 10.0):
        if host != "127.0.0.1":
            raise ValueError("El cliente solo habla con la API local en 127.0.0.1")
        self.base = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout
        # Evitar que un proxy del sistema intercepte tráfico a 127.0.0.1
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_local_install(cls, data_dir: Optional[str] = None, **kw) -> "CauceClient":
        from .config import default_data_dir
        from .secrets_store import read_secret
        d = data_dir or default_data_dir()
        info_path = os.path.join(d, "api.json")
        port = DEFAULT_API_PORT
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as fh:
                port = int(json.load(fh).get("port", port))
        return cls(read_secret(os.path.join(d, "api.token")), port=port, **kw)

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + self.API + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8")).get("error", {})
            except Exception:
                err = {}
            raise CauceAPIError(e.code, err.get("code", "http_error"), err.get("message", str(e))) from None

    # --- consultas ------------------------------------------------------
    def ping(self) -> dict:
        return self._call("GET", "/ping")

    def diagnostics(self) -> dict:
        return self._call("GET", "/diagnostics")

    def connections(self, refresh: bool = False) -> list[dict]:
        return self._call("GET", "/connections" + ("?refresh=1" if refresh else ""))["connections"]

    def connection(self, connection_id: str) -> dict:
        return self._call("GET", f"/connections/{quote(connection_id)}")

    # --- credenciales (solo administración) -----------------------------
    def create_token(self, name: str, scope: str = "read") -> dict:
        return self._call("POST", "/tokens", {"name": name, "scope": scope})

    def tokens(self) -> list[dict]:
        return self._call("GET", "/tokens")["tokens"]

    def tokens_status(self) -> dict:
        """Estado del almacén: ``locked`` (motivo o None), ``pending_revocations`` y ``lock_recorded_on_disk``."""
        d = self._call("GET", "/tokens")
        return {k: v for k, v in d.items() if k != "tokens"}

    def recover_tokens(self) -> dict:
        return self._call("POST", "/tokens/recover", {})

    def revoke_token(self, token_id: str) -> dict:
        return self._call("DELETE", f"/tokens/{quote(token_id)}")

    def events(self, since: int = 0, kind: Optional[str] = None, limit: int = 200) -> dict:
        q = f"?since={since}&limit={limit}" + (f"&kind={quote(kind)}" if kind else "")
        return self._call("GET", "/events" + q)

    # --- rutas ----------------------------------------------------------
    def request_route(self, owner: str, purpose: str, connection_id: Optional[str] = None,
                      allow_failover: bool = True, allow_failback: bool = True) -> dict:
        return self._call("POST", "/routes", {"owner": owner, "purpose": purpose, "connection_id": connection_id,
                                               "allow_failover": allow_failover, "allow_failback": allow_failback})

    def route(self, lease_id: str) -> dict:
        return self._call("GET", f"/routes/{lease_id}")

    def release_route(self, lease_id: str, owner: str) -> dict:
        return self._call("DELETE", f"/routes/{lease_id}?owner={quote(owner)}")

    def connect_via(self, route: dict, address: tuple[str, int], timeout: float = 10.0) -> socket.socket:
        """Abre un socket TCP enlazado a la IP de origen de la ruta. Vuelve a
        consultar la ruta antes de conectar por si el motor conmutó."""
        current = self.route(route["id"])
        ip = current["bind"]["source_ip"]
        s = socket.create_connection(address, timeout=timeout, source_address=(ip, 0))
        if s.getsockname()[0] != ip:
            s.close()
            raise CauceAPIError(0, "route_mismatch", f"El socket salió por {s.getsockname()[0]} y no por {ip}")
        return s

    # --- tareas y control -----------------------------------------------
    def select(self, connection_id: Optional[str]) -> dict:
        return self._call("POST", "/selection", {"connection_id": connection_id})

    def start_task(self, spec: dict) -> dict:
        return self._call("POST", "/tasks", spec)

    def task(self, task_id: str) -> dict:
        return self._call("GET", f"/tasks/{task_id}")

    def stop_task(self, task_id: str, reason: str = "") -> dict:
        return self._call("POST", f"/tasks/{task_id}/stop", {"reason": reason})

    def stop_all(self, reason: str = "") -> dict:
        return self._call("POST", "/stop-all", {"reason": reason})

    def rotation(self) -> list[dict]:
        return self._call("GET", "/rotation")["policies"]

    def create_rotation(self, name: str, pool: list[str], interval_s: float = 60, max_changes_per_hour: int = 20) -> dict:
        return self._call("POST", "/rotation", {"name": name, "pool": pool, "interval_s": interval_s,
                                                 "max_changes_per_hour": max_changes_per_hour})

    def set_rotation(self, name: str, action: str, reason: str = "") -> dict:
        return self._call("POST", f"/rotation/{quote(name)}/{action}", {"reason": reason})

    def shutdown(self) -> dict:
        return self._call("POST", "/shutdown", {})


class CauceReadClient:
    """Cliente de SOLO LECTURA para consumidores con una credencial ``read``.

    No tiene métodos de administración, rutas, tareas ni ``refresh``: aunque los tuviera, el servidor
    respondería 403. Solo llama a /ping, /status/connectivity, /status/routers y /status/events.

    Regla de fallo seguro de ``connectivity_or_unknown``:
    - Motor sin respuesta (apagado, tiempo agotado, JSON ilegible) o error 5xx (p. ej. 503 ``credentials_locked``):
      devuelve ``state="engine_unreachable"`` con ``usable=None`` («sin información»; ``reason`` = código del
      error o ``engine_unreachable``). Eso NO significa que el equipo no tenga internet.
    - 401 (token inválido o revocado), 403 (permiso insuficiente) y 400 (petición mal formada): son errores de
      autenticación/configuración, NO de conectividad. Se propagan como ``CauceAPIError`` y el cliente queda
      DETENIDO (``halted``): las llamadas siguientes de ``connectivity_or_unknown`` repiten el error SIN enviar
      peticiones, hasta que el operador cambie la credencial y llame a ``reset()``.
    - Motor funcionando: devuelve el estado real (``usable``, ``degraded``, ``unavailable``, ``unknown``) sin tocarlo.
    """

    HALT_STATUSES = (400, 401, 403)

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = DEFAULT_API_PORT, timeout: float = 2.0):
        self._c = CauceClient(token, host, port, timeout)
        self.halted: Optional[CauceAPIError] = None

    def reset(self) -> None:
        """Reanuda las consultas tras un ``halted`` (el operador ya corrigió la credencial o el permiso)."""
        self.halted = None

    @classmethod
    def from_token_file(cls, token_path: str, port: int = DEFAULT_API_PORT, **kw) -> "CauceReadClient":
        """Lee el token de un archivo creado con ``cauce token create --out`` (protegido con DPAPI en Windows)."""
        from .secrets_store import read_secret
        return cls(read_secret(token_path), port=port, **kw)

    def ping(self) -> dict:
        return self._c.ping()

    def connectivity(self) -> dict:
        return self._c._call("GET", "/status/connectivity")

    def routers(self) -> dict:
        return self._c._call("GET", "/status/routers")

    def events(self, since: int = 0, limit: int = 200) -> dict:
        return self._c._call("GET", f"/status/events?since={int(since)}&limit={int(limit)}")

    def sessions(self) -> dict:
        """Estado de las sesiones de navegador registradas (``cauce.sessions/1``)."""
        return self._c._call("GET", "/status/sessions")

    def connectivity_or_unknown(self) -> dict:
        if self.halted is not None:
            raise self.halted                       # sin tocar la red: reintentar una credencial rota no la arregla
        try:
            return self.connectivity()
        except CauceAPIError as e:
            if e.status in self.HALT_STATUSES:
                self.halted = e
                raise                                # autenticación/configuración: nunca se disfraza de «sin información»
            if e.status >= 500:                      # 5xx (p. ej. 503 credentials_locked): sin información, no «sin internet»
                return self._unknown(e.code)
            raise                                    # otro 4xx inesperado: se ve, pero sin detener (no es de credencial)
        except (OSError, ValueError):               # motor ausente, rechazo de conexión, tiempo agotado, JSON ilegible
            return self._unknown("engine_unreachable")

    @staticmethod
    def _unknown(reason: str) -> dict:
        return {"schema": "cauce.connectivity/1", "state": "engine_unreachable", "usable": None,
                "reason": reason, "connections": []}


class CauceSessionClient:
    """Cliente para el DUEÑO de una sesión de navegador (p. ej. Cauce Forms), con una credencial ``session``.

    Solo informa estado: registrar la sesión, enviar latidos ``ok``/``error`` y cerrarla.
    El motor rechaza cualquier dato extra (URL, cookies, contenido de páginas): no los envíes.
    Una credencial solo puede tener UNA sesión activa; usa una credencial distinta por sesión.
    """

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = DEFAULT_API_PORT, timeout: float = 2.0):
        self._c = CauceClient(token, host, port, timeout)
        self.session_id: Optional[str] = None

    @classmethod
    def from_token_file(cls, token_path: str, port: int = DEFAULT_API_PORT, **kw) -> "CauceSessionClient":
        from .secrets_store import read_secret
        return cls(read_secret(token_path), port=port, **kw)

    def register(self, label: str, connection_id: Optional[str] = None, heartbeat_timeout_s: float = 30.0) -> dict:
        body = {"label": label, "authorized_by_user": True, "heartbeat_timeout_s": heartbeat_timeout_s}
        if connection_id is not None:
            body["connection_id"] = connection_id
        out = self._c._call("POST", "/sessions", body)
        self.session_id = out["id"]
        return out

    def _sid(self) -> str:
        if not self.session_id:
            raise RuntimeError("Primero register()")
        return self.session_id

    def heartbeat(self, ok: bool = True, error_code: Optional[str] = None, operation: Optional[str] = None) -> dict:
        body: dict[str, Any] = {"status": "ok" if ok else "error"}
        if not ok:
            body["error_code"] = error_code or "other"
        if operation is not None:
            body["operation"] = operation          # "idle" | "in_progress"
        return self._c._call("POST", f"/sessions/{self._sid()}/heartbeat", body)

    def get(self) -> dict:
        return self._c._call("GET", f"/sessions/{self._sid()}")

    def close(self) -> dict:
        out = self._c._call("DELETE", f"/sessions/{self._sid()}")
        self.session_id = None
        return out
