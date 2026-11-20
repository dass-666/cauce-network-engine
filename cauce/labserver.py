"""Servidor HTTP de laboratorio propio (solo 127.0.0.1 por defecto).

Sirve para comprobar rutas con tráfico real: responde en la cabecera
``X-Cauce-Observed-Ip`` la IP de origen que vio, y permite simular respuestas
de limitación.

Rutas:
  GET  /ok          200
  GET  /limit       429 con Retry-After: 60
  POST /write       200, cuenta escrituras (para detectar duplicados)
  GET  /ip          IP de origen observada, texto plano (para egress_check_url)
  GET  /stats       contador de escrituras por Idempotency-Key
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LabServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8770):
        self.writes: dict[str, int] = {}
        self.requests: list[dict] = []
        lab = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, obj, extra=None):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Cauce-Observed-Ip", self.client_address[0])
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _log(self):
                lab.requests.append({"method": self.command, "path": self.path, "client_ip": self.client_address[0]})

            def do_GET(self):
                self._log()
                if self.path.startswith("/limit"):
                    return self._reply(429, {"error": "rate limited"}, {"Retry-After": "60"})
                if self.path.startswith("/ip"):
                    data = self.client_address[0].encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path.startswith("/stats"):
                    return self._reply(200, {"writes": lab.writes})
                return self._reply(200, {"ok": True, "observed_ip": self.client_address[0]})

            def do_POST(self):
                self._log()
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                key = self.headers.get("Idempotency-Key") or f"anon-{len(lab.requests)}"
                lab.writes[key] = lab.writes.get(key, 0) + 1
                return self._reply(200, {"ok": True, "key": key})

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.port = self.httpd.server_address[1]
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
