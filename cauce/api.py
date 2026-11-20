"""API HTTP local, versionada (/api/v1), solo en 127.0.0.1.

Seguridad:
- Escucha únicamente en 127.0.0.1 (validado en config y aquí).
- Token de sesión aleatorio por arranque (permiso ``admin``), exigido en todas las
  rutas /api/v1 salvo /api/v1/ping. Se envía como ``Authorization: Bearer <token>``.
- Credenciales ``read`` por sistema consumidor (cauce/tokens.py). Cada ruta declara
  en el servidor el permiso que exige (por defecto ``admin``); si falta, 403
  ``insufficient_scope``. Un token ``read`` solo llega a /status/*.
- Comprobación de cabecera Host (protección frente a DNS rebinding) y de
  Origin en peticiones que modifican estado (protección frente a CSRF desde
  páginas web abiertas en el navegador).
- Cuerpos JSON limitados a 256 KiB.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from . import API_VERSION, __version__
from .engine import Engine, EngineError
from .policy import PolicyError
from .routing import RouteError
from .status import MAX_EVENTS, connectivity_view, events_view
from .tokens import SCOPE_ADMIN, SCOPE_READ, AccessLog, Principal, TokenError, TokenStore

_RANK = {SCOPE_READ: 1, SCOPE_ADMIN: 2}

MAX_BODY = 256 * 1024
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


Route = tuple[str, re.Pattern, Callable, str]   # método, patrón, función, permiso exigido


class CauceAPI:
    def __init__(self, engine: Engine, host: str = "127.0.0.1", port: int = 8765, token: Optional[str] = None):
        if host != "127.0.0.1":
            raise ValueError("La API solo puede escuchar en 127.0.0.1")
        self.engine = engine
        self.token = token or secrets.token_urlsafe(32)
        self.host = host
        data_dir = engine.cfg.get("data_dir")
        self.tokens = TokenStore(os.path.join(data_dir, "tokens.dat") if data_dir else None)
        self.access = AccessLog(os.path.join(data_dir, "access.jsonl") if data_dir else None)
        self.routes: list[Route] = []
        self._register()
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"Cauce/{__version__}"
            sys_version = ""

            def log_message(self, fmt, *args):  # sin ruido en consola; los errores van a auditoría
                pass

            def do_GET(self):
                api._dispatch(self, "GET")

            def do_POST(self):
                api._dispatch(self, "POST")

            def do_DELETE(self):
                api._dispatch(self, "DELETE")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None
        self.shutdown_requested = threading.Event()

    # ------------------------------------------------------------------
    def _route(self, method: str, pattern: str, scope: str = SCOPE_ADMIN):
        """Registra una ruta. El permiso por defecto es ``admin``: una ruta nueva nunca queda abierta a ``read`` por olvido."""
        def deco(fn):
            self.routes.append((method, re.compile("^" + pattern + "$"), fn, scope))
            return fn
        return deco

    def _register(self) -> None:
        e = self.engine
        r = self._route
        P = f"/api/{API_VERSION}"

        @r("GET", P + "/ping")
        def ping(req, m, q, body):
            return {"ok": True, "service": "cauce-network-engine", "version": __version__, "api": API_VERSION}

        @r("GET", P + "/status/connectivity", SCOPE_READ)
        def status_connectivity(req, m, q, body):
            self._only_params(q, ())
            return connectivity_view(e, time.time())

        @r("GET", P + "/status/events", SCOPE_READ)
        def status_events(req, m, q, body):
            self._only_params(q, ("since", "limit"))
            since = int(q.get("since", ["0"])[0] or 0)
            limit = int(q.get("limit", ["200"])[0] or 200)
            if since < 0 or limit < 1:
                raise ApiError(400, "invalid_request", "since >= 0 y limit >= 1")
            return events_view(e, since, min(limit, MAX_EVENTS))

        @r("GET", P + "/tokens")
        def tokens_list(req, m, q, body):
            return dict(self.tokens.status(), tokens=self.tokens.list())

        @r("POST", P + "/tokens")
        def tokens_create(req, m, q, body):
            rec, secret = self.tokens.create(str(body.get("name") or ""), str(body.get("scope") or SCOPE_READ))
            e.audit.record("token.created", f"Credencial creada para {rec['name']} (permiso {rec['scope']})",
                           grant_id=rec["id"], name=rec["name"], scope=rec["scope"])
            return dict(rec, token=secret, note="Este es el único momento en que se muestra el token."), 201

        @r("POST", P + "/tokens/recover")
        def tokens_recover(req, m, q, body):
            res = self.tokens.recover()
            if res["recovered"]:
                e.audit.record("token.recovered", "Almacén de credenciales verificado; bloqueo levantado")
            return res

        @r("DELETE", P + r"/tokens/(?P<tid>[0-9a-f]{8})")
        def tokens_revoke(req, m, q, body):
            try:
                rec = self.tokens.revoke(m["tid"])
            except TokenError as err:
                if err.code == "revocation_not_persisted":   # sin secretos: solo el id
                    e.audit.record("token.revocation_failed", "Revocación NO persistida; credenciales read bloqueadas",
                                   grant_id=m["tid"])
                raise
            e.audit.record("token.revoked", f"Credencial revocada: {rec['name']}", grant_id=rec["id"], name=rec["name"])
            return rec

        @r("GET", P + "/diagnostics")
        def diag(req, m, q, body):
            return e.diagnostics()

        @r("GET", P + "/connections")
        def conns(req, m, q, body):
            if q.get("refresh", ["0"])[0] == "1":
                e.refresh()
            return {"connections": [e.connection_view(c) for c in e.connections()],
                    "operator_selected": e.routes.operator_choice}

        @r("GET", P + r"/connections/(?P<cid>[\w.:-]+)")
        def conn(req, m, q, body):
            c = e.connection(m["cid"])
            if not c:
                raise ApiError(404, "unknown_connection", "Conexión inexistente")
            return e.connection_view(c)

        @r("POST", P + r"/connections/(?P<cid>[\w.:-]+)/check")
        def check(req, m, q, body):
            c = e.connection(m["cid"])
            if not c:
                raise ApiError(404, "unknown_connection", "Conexión inexistente")
            e.health.check(c)
            return e.connection_view(c)

        @r("POST", P + "/selection")
        def select(req, m, q, body):
            cid = body.get("connection_id")
            if cid is None:
                e.routes.operator_choice = None
                e.audit.record("route.selected", "Selección del operador anulada (automática)", actor="api")
                return {"operator_selected": None}
            e.routes.select(cid, actor=str(body.get("actor", "api")))
            return {"operator_selected": cid}

        @r("GET", P + "/routes")
        def leases(req, m, q, body):
            return {"routes": [l.to_dict() for l in e.routes.leases()]}

        @r("POST", P + "/routes")
        def acquire(req, m, q, body):
            lease = e.routes.acquire(str(body.get("owner") or ""), str(body.get("purpose") or "externa"),
                                     body.get("connection_id"), bool(body.get("allow_failover", True)),
                                     bool(body.get("allow_failback", True)))
            return self._route_payload(lease), 201

        @r("GET", P + r"/routes/(?P<lid>ls_[0-9a-f]+)")
        def get_lease(req, m, q, body):
            l = e.routes.get(m["lid"])
            if not l:
                raise ApiError(404, "unknown_lease", "Lease inexistente")
            return self._route_payload(l)

        @r("DELETE", P + r"/routes/(?P<lid>ls_[0-9a-f]+)")
        def release(req, m, q, body):
            owner = q.get("owner", [""])[0] or body.get("owner", "")
            e.routes.release(m["lid"], owner)
            return {"released": m["lid"]}

        @r("GET", P + "/tasks")
        def tasks(req, m, q, body):
            return {"tasks": [t.to_dict() for t in e.executor.all()]}

        @r("POST", P + "/tasks")
        def start(req, m, q, body):
            t = e.start_task(body)
            return t.to_dict(), 202

        @r("GET", P + r"/tasks/(?P<tid>tk_[0-9a-f]+)")
        def task(req, m, q, body):
            t = e.executor.get(m["tid"])
            if not t:
                raise ApiError(404, "unknown_task", "Tarea inexistente")
            return t.to_dict()

        @r("POST", P + r"/tasks/(?P<tid>tk_[0-9a-f]+)/stop")
        def stop(req, m, q, body):
            try:
                return e.executor.stop(m["tid"], str(body.get("reason") or "detenida vía API")).to_dict()
            except KeyError:
                raise ApiError(404, "unknown_task", "Tarea inexistente")

        @r("POST", P + "/stop-all")
        def stop_all(req, m, q, body):
            return e.stop_all(str(body.get("reason") or "parada general vía API"))

        @r("GET", P + "/rotation")
        def rot(req, m, q, body):
            return {"policies": [p.to_dict() for p in e.rotation.all()]}

        @r("POST", P + "/rotation")
        def rot_create(req, m, q, body):
            p = e.rotation.create(str(body.get("name") or ""), list(body.get("pool") or []),
                                  float(body.get("interval_s", 60)), int(body.get("max_changes_per_hour", 20)))
            return p.to_dict(), 201

        @r("POST", P + r"/rotation/(?P<name>[\w-]+)/(?P<action>activate|pause|stop)")
        def rot_state(req, m, q, body):
            st = {"activate": "active", "pause": "paused", "stop": "stopped"}[m["action"]]
            return e.rotation.set_state(m["name"], st, str(body.get("reason") or "operador")).to_dict()

        @r("GET", P + "/events")
        def events(req, m, q, body):
            since = int(q.get("since", ["0"])[0] or 0)
            kind = q.get("kind", [None])[0]
            limit = min(int(q.get("limit", ["200"])[0] or 200), 1000)
            return {"events": e.audit.events(since, kind, limit), "last_seq": e.audit.last_seq}

        @r("POST", P + "/shutdown")
        def shutdown(req, m, q, body):
            self.shutdown_requested.set()
            return {"shutting_down": True}, 202

    def enable_sim_control(self, sim) -> None:
        """Solo en modo simulación: permite cambiar el estado de una conexión ficticia."""
        e = self.engine

        @self._route("POST", f"/api/{API_VERSION}" + r"/sim/connections/(?P<cid>[\w.:-]+)/state")
        def sim_state(req, m, q, body):
            st = body.get("state")
            if m["cid"] not in sim.links or st not in ("up", "down", "disconnected"):
                raise ApiError(400, "invalid_request", "conexión simulada o estado inválido")
            sim.set_state(m["cid"], st)
            e.audit.record("sim.state", f"[SIMULACIÓN] {m['cid']} -> {st}", connection_id=m["cid"], state=st)
            e.refresh()
            c = e.connection(m["cid"])
            if c:
                e.health.check(c)
            return {"connection_id": m["cid"], "state": st}

    def _route_payload(self, lease) -> dict[str, Any]:
        d = lease.to_dict()
        d["bind"] = {"source_ip": lease.local_ip, "source_port": 0,
                     "how": "Enlaza tu socket a source_ip antes de conectar (p.ej. socket.create_connection(dst, source_address=(source_ip, 0)))."}
        return d

    # ------------------------------------------------------------------
    def _host_ok(self, h: BaseHTTPRequestHandler) -> bool:
        host = (h.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _origin_ok(self, h: BaseHTTPRequestHandler) -> bool:
        origin = h.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    @staticmethod
    def _only_params(q: dict, allowed: tuple[str, ...]) -> None:
        """Las vistas de lectura rechazan parámetros desconocidos (p. ej. ``refresh=1``) en vez de ignorarlos."""
        bad = sorted(set(q) - set(allowed))
        if bad:
            raise ApiError(400, "unsupported_parameter", f"Parámetro no admitido en esta vista: {', '.join(bad)}")

    def _authenticate(self, h: BaseHTTPRequestHandler) -> Principal:
        a = h.headers.get("Authorization") or ""
        presented = a[7:].strip() if a.startswith("Bearer ") else ""
        if presented and hmac.compare_digest(presented, self.token):
            return Principal(SCOPE_ADMIN, "session", "operator")
        try:
            return self.tokens.verify(presented)
        except TokenError as e:
            raise ApiError(e.status, e.code, e.message)

    def _send(self, h: BaseHTTPRequestHandler, status: int, payload: Any, ctype: str = "application/json; charset=utf-8") -> None:
        self._log_access(h, status)
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'")
        h.end_headers()
        h.wfile.write(data)

    def _error(self, h, status: int, code: str, message: str) -> None:
        h._cauce_code = code
        self._send(h, status, {"error": {"code": code, "message": message, "status": status}})

    def _dispatch(self, h: BaseHTTPRequestHandler, method: str) -> None:
        h._cauce_principal = None
        h._cauce_code = ""
        h._cauce_method = method
        self._dispatch_inner(h, method)

    def _log_access(self, h: BaseHTTPRequestHandler, status: int) -> None:
        """Se anota ANTES de enviar la respuesta (así el registro existe cuando el cliente la recibe)."""
        url = urlsplit(getattr(h, "path", ""))
        if url.path.startswith("/api/"):   # solo se anotan las claves de la consulta, nunca los valores
            keys = ",".join(sorted(parse_qs(url.query)))
            self.access.record(getattr(h, "_cauce_principal", None), getattr(h, "_cauce_method", "?"),
                               url.path + (f"?{keys}" if keys else ""), status, getattr(h, "_cauce_code", ""))

    def _dispatch_inner(self, h: BaseHTTPRequestHandler, method: str) -> None:
        try:
            if not self._host_ok(h):
                return self._error(h, 421, "bad_host", "Cabecera Host no permitida")
            url = urlsplit(h.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                t = parse_qs(url.query).get("t", [""])[0]
                if not hmac.compare_digest(t, self.token):
                    return self._send(h, 401, "<h1>Cauce</h1><p>Abre el panel con <code>cauce panel</code> "
                                      "(incluye el token de sesión en la URL).</p>".encode("utf-8"), "text/html; charset=utf-8")
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                    html = fh.read().replace(b"__CAUCE_TOKEN__", self.token.encode())
                return self._send(h, 200, html, "text/html; charset=utf-8")
            if not url.path.startswith(f"/api/{API_VERSION}/"):
                return self._error(h, 404, "not_found", "Ruta inexistente")
            if url.path != f"/api/{API_VERSION}/ping":
                h._cauce_principal = principal = self._authenticate(h)
                # Autorización en el servidor, ANTES de leer el cuerpo o ejecutar nada.
                for m_, pat, _fn, need in self.routes:
                    if m_ == method and pat.match(url.path):
                        if _RANK[principal.scope] < _RANK[need]:
                            raise ApiError(403, "insufficient_scope",
                                           f"Esta credencial ({principal.scope}) no tiene permiso {need} para {method} {url.path}")
                        break
            if method != "GET" and not self._origin_ok(h):
                return self._error(h, 403, "bad_origin", "Origin no permitido")
            body: dict = {}
            if method in ("POST", "DELETE"):
                n = int(h.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    return self._error(h, 413, "too_large", "Cuerpo demasiado grande")
                if n:
                    try:
                        body = json.loads(h.rfile.read(n).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        return self._error(h, 400, "invalid_json", "JSON inválido")
                    if not isinstance(body, dict):
                        return self._error(h, 400, "invalid_json", "Se esperaba un objeto JSON")
            q = parse_qs(url.query)
            for m_, pat, fn, _need in self.routes:
                if m_ != method:
                    continue
                match = pat.match(url.path)
                if match:
                    out = fn(h, match.groupdict(), q, body)
                    status = 200
                    if isinstance(out, tuple):
                        out, status = out
                    return self._send(h, status, out)
            return self._error(h, 404, "not_found", "Ruta inexistente")
        except ApiError as e:
            self._error(h, e.status, e.code, e.message)
        except TokenError as e:
            self._error(h, e.status, e.code, e.message)
        except EngineError as e:
            self._error(h, e.status, e.code, e.message)
        except RouteError as e:
            status = {"not_owner": 403, "unknown_lease": 404, "unknown_connection": 404}.get(e.code, 409)
            self._error(h, status, e.code, e.message)
        except PolicyError as e:
            self._error(h, 409, "policy_error", str(e))
        except (ValueError, TypeError) as e:
            self._error(h, 400, "invalid_request", str(e))
        except Exception as e:
            self.engine.audit.record("error", f"Error interno de API: {e!r}", path=h.path)
            self._error(h, 500, "internal_error", "Error interno; ver registro de errores")

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="cauce-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
