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


class CauceAPIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class CauceClient:
    API = "/api/v1"

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8765, timeout: float = 10.0):
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
        port = 8765
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
