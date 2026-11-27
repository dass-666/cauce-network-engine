"""Servidor HTTP de laboratorio propio (solo 127.0.0.1 por defecto).

Sirve para comprobar rutas con tráfico real: responde en la cabecera
``X-Cauce-Observed-Ip`` la IP de origen que vio, y permite simular respuestas
de limitación.

Rutas:
  GET  /ok          200
  GET  /limit       429 con Retry-After: 60
  POST /write[?op=X] 200, cuenta escrituras por Idempotency-Key u ``op`` (para detectar duplicados)
  GET  /ip          IP de origen observada, texto plano (para egress_check_url)
  GET  /stats       contador de escrituras por Idempotency-Key
  GET  /session?sid=X        página que late cada segundo (prueba de sesión de navegador, 0.2.0)
  GET  /beat?sid=X&n=N       latido de esa página (lo envía el propio navegador)
  GET  /session-report?sid=X latidos recibidos, huecos y la IP de origen de cada tramo

El servidor es PROPIO: arráncalo en un equipo tuyo. Por defecto solo escucha en 127.0.0.1; para que lo
alcance el equipo de pruebas desde internet hay que pasar ``--host 0.0.0.0`` a propósito.
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LabServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8770, clock=time.time):
        self.writes: dict[str, int] = {}
        self.beats: dict[str, list[tuple[float, int, str]]] = {}     # sid -> [(ts, n, ip)]
        self.clock = clock
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
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                sid = (q.get("sid") or "")[:40]
                if u.path == "/session":
                    data = _SESSION_PAGE.replace("__SID__", json.dumps(sid)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if u.path == "/beat" and sid:
                    try:
                        n = int(q.get("n", "0"))
                    except ValueError:
                        n = -1
                    with lab._lock:
                        lab.beats.setdefault(sid, []).append((lab.clock(), n, self.client_address[0]))
                    return self._reply(200, {"ok": True})
                if u.path == "/session-report" and sid:
                    return self._reply(200, lab.session_report(sid))
                return self._reply(200, {"ok": True, "observed_ip": self.client_address[0]})

            def do_POST(self):
                self._log()
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                op = parse_qs(urlsplit(self.path).query).get("op", [None])[0]
                key = self.headers.get("Idempotency-Key") or (f"op-{op[:60]}" if op else f"anon-{len(lab.requests)}")
                lab.writes[key] = lab.writes.get(key, 0) + 1
                return self._reply(200, {"ok": True, "key": key})

        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer((host, port), H)
        self.port = self.httpd.server_address[1]
        self._t = None

    def start(self):
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()
        return self

    def session_report(self, sid: str, gap_s: float = 3.0) -> dict:
        """Huecos entre latidos mayores que ``gap_s`` y tramos por IP de origen (cambio de salida visto por el servidor)."""
        with self._lock:
            beats = list(self.beats.get(sid, []))
        gaps, segments = [], []
        for prev, cur in zip(beats, beats[1:]):
            if cur[0] - prev[0] > gap_s:
                gaps.append({"from": round(prev[0], 3), "to": round(cur[0], 3), "seconds": round(cur[0] - prev[0], 1),
                             "beats_lost": max(0, cur[1] - prev[1] - 1) if cur[1] >= 0 and prev[1] >= 0 else None})
        for ts, _n, ip in beats:
            if not segments or segments[-1]["ip"] != ip:
                segments.append({"ip": ip, "from": round(ts, 3), "beats": 0})
            segments[-1]["beats"] += 1
            segments[-1]["to"] = round(ts, 3)
        # si el contador del navegador vuelve a 1 la página se recargó (la sesión de la pestaña se perdió)
        reloads = sum(1 for prev, cur in zip(beats, beats[1:]) if 0 <= cur[1] < prev[1])
        return {"sid": sid, "beats": len(beats), "gaps": gaps, "max_gap_s": max((g["seconds"] for g in gaps), default=0.0),
                "page_reloads": reloads, "source_ips": segments,
                "last_beat_age_s": round(self.clock() - beats[-1][0], 1) if beats else None}

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# Página mínima: late cada segundo con fetch() y reintenta sola. No guarda cookies ni datos.
_SESSION_PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><title>Cauce · prueba de sesión</title>
<body style="font-family:system-ui;margin:2rem">
<h1>Prueba de sesión de navegador</h1>
<p>No cierres esta pestaña durante la prueba. Late cada segundo contra tu servidor de laboratorio.</p>
<p>Latidos enviados: <b id="n">0</b> · fallidos: <b id="f">0</b> · último estado: <b id="s">-</b></p>
<script>
const sid = __SID__; let n = 0, f = 0;
async function beat() {
  n += 1; document.getElementById('n').textContent = n;
  try {
    const r = await fetch('/beat?sid=' + encodeURIComponent(sid) + '&n=' + n, {cache: 'no-store'});
    document.getElementById('s').textContent = r.ok ? 'ok' : ('HTTP ' + r.status);
  } catch (e) { f += 1; document.getElementById('f').textContent = f; document.getElementById('s').textContent = 'sin red'; }
}
setInterval(beat, 1000); beat();
</script></body></html>"""
