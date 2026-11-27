"""Paquete de validación FÍSICA guiada (Windows 11 con dos enlaces reales).

``python -m cauce fieldcheck --lab-url http://<tu-servidor>:8770``

Qué comprueba (cada punto queda en el informe como ok / fallo / omitido, con su evidencia):

  F1  Entorno (Windows, Python, privilegios) y rutas por defecto ANTES de empezar (solo lectura).
  F2  Detección de al menos dos enlaces utilizables con salidas distintas.
  F3  Estado de cada enlace (salud, latencia, disponibilidad) contra TU servidor.
  F4  Sesión de navegador: abre una página de TU servidor que late cada segundo.
  F5  Tarea larga por el enlace principal con escrituras con y sin clave de idempotencia.
  F6  Pérdida real: tú desconectas el enlace principal; se mide la detección y la conmutación.
  F7  Recuperación: tú lo reconectas; se mide la recuperación y el retorno.
  F8  Sin operaciones repetidas: TU servidor cuenta cada escritura (debe ser exactamente 1).
  F9  Qué le pasó a la sesión del navegador (huecos, recargas, cambio de IP vista por el servidor).
  F10 Cadena de la auditoría íntegra.
  F11 Los tres motores responden a la vez (Monitor, Forms, Network Engine), sin arrancarlos.
  F12 Rutas por defecto DESPUÉS: el motor no las cambió (las diferencias las produce Windows al desconectar).

Reglas que no cambian:
- Solo contra TU servidor de laboratorio (``--lab-url``): sondeos, tareas y página de sesión van allí.
- No toca la tabla de rutas, ni adaptadores, ni el firewall: la desconexión la haces tú a mano.
- No usa la instalación de Network Engine: el motor de la prueba vive en la carpeta del informe.
- La rotación sigue apagada. Un 429/403 detiene la tarea igual que siempre.
- ``--simulate`` ejecuta el mismo guion con el simulador (para CI); su informe dice SIMULADO y no vale
  como validación física.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from . import DEFAULT_API_PORT, __version__

OK, FAIL, SKIP, INFO = "ok", "fallo", "omitido", "info"
ENGINES = {"Monitor": ("127.0.0.1", 8000, "/api/info"), "Forms": ("127.0.0.1", 8765, "/"),
           "Network Engine": ("127.0.0.1", DEFAULT_API_PORT, "/api/v1/ping")}
# Puertos del laboratorio integrado (scripts/laboratorio_integrado/versiones.json): distintos de los normales.
ENGINES_LAB = {"Monitor": ("127.0.0.1", 8100, "/api/info"), "Forms": ("127.0.0.1", 8865, "/"),
               "Network Engine": ("127.0.0.1", 8880, "/api/v1/ping")}


def mask_ip(ip: Optional[str]) -> Optional[str]:
    """El informe no guarda IP completas: basta con ver que cambian de un proveedor a otro."""
    if not ip or ip.count(".") != 3:
        return ip
    return ".".join(ip.split(".")[:3]) + ".x"


def default_routes() -> list[dict]:
    """Rutas por defecto IPv4 del sistema, SOLO LECTURA (Get-NetRoute / ip route)."""
    try:
        if sys.platform == "win32":
            ps = ("Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                  "Select-Object ifIndex, NextHop, RouteMetric | ConvertTo-Json -Compress")
            out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
            data = json.loads(out) if out else []
            data = data if isinstance(data, list) else [data]
            return sorted(({"if": d.get("ifIndex"), "next_hop": mask_ip(d.get("NextHop")), "metric": d.get("RouteMetric")}
                           for d in data), key=lambda r: (str(r["if"]), str(r["next_hop"])))
        out = subprocess.run(["ip", "-j", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=10).stdout
        return sorted(({"if": d.get("dev"), "next_hop": mask_ip(d.get("gateway")), "metric": d.get("metric")}
                       for d in json.loads(out or "[]")), key=lambda r: (str(r["if"]), str(r["next_hop"])))
    except Exception as e:  # noqa: BLE001
        return [{"error": type(e).__name__}]


def probe_engines(engines: dict = ENGINES, timeout: float = 2.0) -> dict[str, dict]:
    out = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, (host, port, path) in engines.items():
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError:
            out[name] = {"port": port, "listening": False, "http": None}
            continue
        try:
            with opener.open(f"http://{host}:{port}{path}", timeout=timeout) as r:
                out[name] = {"port": port, "listening": True, "http": r.status}
        except urllib.error.HTTPError as e:          # 401/404 también prueba que el proceso responde
            out[name] = {"port": port, "listening": True, "http": e.code}
        except Exception as e:  # noqa: BLE001
            out[name] = {"port": port, "listening": True, "http": None, "error": type(e).__name__}
    return out


class Report:
    def __init__(self, simulated: bool):
        self.simulated = simulated
        self.started = time.time()
        self.checks: list[dict] = []
        self.meta: dict[str, Any] = {}

    def add(self, cid: str, title: str, status: str, detail: str = "", evidence: Any = None) -> dict:
        c = {"id": cid, "title": title, "status": status, "detail": detail, "evidence": evidence}
        self.checks.append(c)
        mark = {OK: "[OK]   ", FAIL: "[FALLO]", SKIP: "[OMIT] ", INFO: "[INFO] "}[status]
        print(f"{mark} {cid} {title}" + (f" — {detail}" if detail else ""), flush=True)
        return c

    @property
    def evidence_type(self) -> str:
        if self.simulated:
            return "SIMULADA (no vale como validación física)"
        if sys.platform != "win32":
            return "FÍSICA EN OTRO SISTEMA (no es Windows 11)"
        return "FÍSICA (Windows, enlaces reales)"

    def to_dict(self) -> dict:
        failed = [c["id"] for c in self.checks if c["status"] == FAIL]
        return {"schema": "cauce.fieldcheck/1", "version": __version__, "evidence_type": self.evidence_type,
                "started": _dt.datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
                "duration_s": round(time.time() - self.started, 1), "result": "FALLOS" if failed else "SIN FALLOS",
                "failed": failed, "meta": self.meta, "checks": self.checks}

    def write(self, out_dir: str) -> tuple[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        d = self.to_dict()
        jp = os.path.join(out_dir, "fieldcheck.json")
        with open(jp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        lines = [f"# Validación física de Cauce Network Engine {d['version']}", "",
                 f"- Tipo de evidencia: **{d['evidence_type']}**", f"- Inicio: {d['started']} · duración {d['duration_s']} s",
                 f"- Resultado: **{d['result']}**" + (f" ({', '.join(d['failed'])})" if d["failed"] else ""), "",
                 "| Punto | Comprobación | Estado | Detalle |", "|---|---|---|---|"]
        for c in self.checks:
            lines.append(f"| {c['id']} | {c['title']} | {c['status']} | {str(c['detail']).replace('|', '/')} |")
        lines += ["", "Las evidencias completas están en `fieldcheck.json` y la auditoría en `engine-data/audit.jsonl`."]
        mp = os.path.join(out_dir, "fieldcheck.md")
        with open(mp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return jp, mp


def _wait(cond: Callable[[], bool], timeout: float, step: float) -> Optional[float]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return round(time.time() - t0, 2)
        time.sleep(step)
    return None


def _http_json(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:       # ruta normal del sistema, hacia TU servidor
        return json.loads(r.read().decode("utf-8"))


class _SimLab:
    """Servidor de laboratorio dentro del simulador: cuenta escrituras como cauce/labserver.py."""

    def __init__(self):
        self.writes: dict[str, int] = {}

    def service(self, link, req):
        from urllib.parse import parse_qs
        u = urlsplit(req.url)
        if req.method == "POST" and u.path == "/write":
            op = parse_qs(u.query).get("op", [None])[0]
            key = req.headers.get("Idempotency-Key") or f"op-{op}"
            self.writes[key] = self.writes.get(key, 0) + 1
        return 200, {}, b'{"ok":true}'


def run(lab_url: str, out_dir: str, simulate: bool = False, ask: Callable[[str], Any] = input,
        steps: int = 240, step_delay_s: float = 1.0, config_path: Optional[str] = None,
        open_browser: bool = True, engines: dict = ENGINES) -> dict:
    from .audit import verify_file
    from .config import load
    from .engine import Engine
    from .executor import Step
    from .models import Health

    u = urlsplit(lab_url.rstrip("/"))
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SystemExit("--lab-url debe ser http(s)://host:puerto de TU servidor de laboratorio")
    lab_port = u.port or (443 if u.scheme == "https" else 80)
    base = f"{u.scheme}://{u.hostname}:{lab_port}"
    rep = Report(simulate)
    data_dir = os.path.join(out_dir, "engine-data")
    fast = simulate
    over = {"data_dir": data_dir,
            "health": {"targets": [f"{u.hostname}:{lab_port}"], "interval_s": 0.05 if fast else 2.0,
                       "max_interval_s": 0.2 if fast else 10.0, "timeout_s": 0.5 if fast else 3.0,
                       "fail_threshold": 3, "recover_threshold": 2},
            "lab": {"allowed_hosts": [u.hostname], "rotation_enabled": False},
            "discovery": {"refresh_s": 0.2 if fast else 5.0},
            "egress_check_url": None if simulate else base + "/ip"}
    cfg = load(config_path, overrides=over)
    simlab = None
    if simulate:
        from .simulator import SimulatedNetwork, three_links
        simlab = _SimLab()
        net = SimulatedNetwork(three_links(), simlab.service)
        engine = Engine(cfg, net, net, mode="simulated")
        poll = 0.02
    else:
        from .discovery import default_provider
        from .net import RealNetwork
        net = None
        engine = Engine(cfg, default_provider(), RealNetwork(), mode="real")
        poll = 0.5
    rep.meta.update({"lab": f"{u.scheme}://{mask_ip(u.hostname) if u.hostname.count('.') == 3 else u.hostname}:{lab_port}",
                     "platform": sys.platform, "python": sys.version.split()[0], "rotation_enabled": engine.rotation_enabled,
                     "global_routes_modified_by_engine": False})

    # F1 ---------------------------------------------------------------
    routes_before = default_routes() if not simulate else [{"if": "sim", "next_hop": "sim", "metric": 0}]
    rep.add("F1", "Entorno y rutas por defecto antes", OK if (simulate or sys.platform == "win32") else INFO,
            f"{sys.platform}, Python {rep.meta['python']}, rotación {'activa' if engine.rotation_enabled else 'apagada'}",
            {"routes_before": routes_before})
    engine.start()
    try:
        # F2 -----------------------------------------------------------
        _wait(lambda: len([c for c in engine.connections() if c.usable]) >= 2, 2 if fast else 20, poll)
        conns = [c for c in engine.connections() if c.usable]
        groups = {c.egress_group for c in conns}
        indep = [c.independence.value for c in conns]
        two = len(conns) >= 2 and len(groups) >= 2
        rep.add("F2", "Dos enlaces utilizables con salidas distintas", OK if two else FAIL,
                f"{len(conns)} utilizables, {len(groups)} salidas distintas",
                [{"name": c.name, "kind": c.kind.value, "independence": i, "public_ip": mask_ip(c.public_ip)}
                 for c, i in zip(conns, indep)])
        if not two:
            rep.add("F3-F9", "Pruebas de conmutación", SKIP, "hacen falta dos enlaces con salidas distintas")
            return _finish(rep, engine, out_dir, routes_before, simulate, engines, verify_file)

        # F3 -----------------------------------------------------------
        _wait(lambda: all(engine.health.state_of(c.id) != Health.UNKNOWN for c in conns), 2 if fast else 30, poll)
        if not fast:
            time.sleep(10)                                       # unas cuantas muestras de latencia
        snap = engine.health.snapshot()
        states = {c.name: {k: snap.get(c.id, {}).get(k) for k in ("state", "avg_latency_ms", "availability_pct")} for c in conns}
        healthy = [n for n, s in states.items() if s["state"] in ("healthy", "degraded")]
        rep.add("F3", "Estado de cada enlace contra tu servidor", OK if len(healthy) >= 2 else FAIL,
                ", ".join(f"{n}: {s['state']} {s['avg_latency_ms']} ms" for n, s in states.items()), states)

        # F4 -----------------------------------------------------------
        sid = "fc-" + secrets.token_hex(4)
        if simulate or not open_browser:
            rep.add("F4", "Sesión de navegador abierta", SKIP, "simulación: sin navegador" if simulate else "--no-browser")
        else:
            import webbrowser
            webbrowser.open(f"{base}/session?sid={sid}")
            ask("Se abrió una página de prueba en el navegador. Déjala abierta y pulsa Enter… ")
            beats = (_http_json(f"{base}/session-report?sid={sid}") or {}).get("beats", 0)
            rep.add("F4", "Sesión de navegador abierta", OK if beats else FAIL, f"{beats} latidos recibidos por tu servidor")

        # F5 -----------------------------------------------------------
        ops = []
        st = []
        for i in range(steps):
            if i % 10 == 5:
                key = f"{sid}-k{i}"
                ops.append(("key", key, i))
                st.append({"method": "POST", "url": f"{base}/write", "idempotency_key": key})
            elif i % 10 == 9:
                op = f"{sid}-n{i}"
                ops.append(("op", "op-" + op, i))
                st.append({"method": "POST", "url": f"{base}/write?op={op}", "idempotent": False})
            else:
                st.append({"method": "GET", "url": f"{base}/ok"})
        task = engine.start_task({"owner": "fieldcheck", "name": "validacion-fisica", "steps": st,
                                  "step_delay_s": step_delay_s, "max_duration_s": max(120, steps * (step_delay_s + 3) * 2),
                                  "step_timeout_s": 2 if fast else 8, "max_attempts": 30 if fast else 10})
        primary = task.lease.connection_id
        pname = (engine.connection(primary) or conns[0]).name
        rep.add("F5", "Tarea larga en marcha por el enlace principal", OK, f"{steps} pasos por «{pname}»",
                {"task_id": task.id, "writes_with_key": sum(1 for o in ops if o[0] == "key"),
                 "writes_without_key": sum(1 for o in ops if o[0] == "op")})

        # F6 -----------------------------------------------------------
        _wait(lambda: task.current_step >= min(15, steps // 4), 60 if not fast else 5, poll)
        t_cut = time.time()
        if simulate:
            net.set_state(primary, "down")
        else:
            ask(f"\n>>> DESCONECTA AHORA «{pname}» (quita el cable o apaga ese enlace en el router/teléfono) y pulsa Enter… ")
        det = _wait(lambda: engine.health.state_of(primary) == Health.DOWN, 5 if fast else 120, poll)
        sw = _wait(lambda: task.lease.connection_id != primary, 5 if fast else 120, poll)
        rep.add("F6", "Pérdida real detectada y tarea conmutada", OK if det is not None and sw is not None else FAIL,
                f"caída detectada en {det} s, conmutación en {sw} s" if det is not None else "no se detectó la caída",
                {"detect_s": det, "switch_s": sw, "to": (engine.connection(task.lease.connection_id) or conns[0]).name})

        # F7 -----------------------------------------------------------
        if fast:
            time.sleep(0.3)
            net.set_state(primary, "up")
        else:
            time.sleep(20)
            ask(f"\n>>> RECONECTA «{pname}» y pulsa Enter… ")
        rec = _wait(lambda: engine.health.state_of(primary) == Health.HEALTHY, 5 if fast else 240, poll)
        back = _wait(lambda: task.lease.connection_id == primary, 5 if fast else 240, poll)
        rep.add("F7", "Recuperación y retorno al enlace principal", OK if rec is not None and back is not None else FAIL,
                f"recuperado en {rec} s, retorno en {back} s", {"recover_s": rec, "failback_s": back,
                                                                  "outage_s": round(time.time() - t_cut, 1)})

        # F8 -----------------------------------------------------------
        _wait(lambda: task.state.value not in ("pending", "running"), 30 if fast else steps * (step_delay_s + 5), poll)
        if task.state.value in ("pending", "running"):
            engine.executor.stop(task.id, "fin del tiempo de la validación")
        try:
            writes = simlab.writes if simlab else _http_json(f"{base}/stats").get("writes", {})
        except Exception as e:  # noqa: BLE001
            writes, err = None, type(e).__name__
        if writes is None:
            rep.add("F8", "Ninguna escritura repetida", FAIL, f"no se pudo leer /stats de tu servidor ({err})")
        else:
            done = task.current_step
            counts = {k: writes.get(k, 0) for _kind, k, _i in ops}
            dup = {k: n for k, n in counts.items() if n > 1}
            # un paso terminado con 200 tiene que constar EXACTAMENTE una vez en tu servidor
            lost = [k for _kind, k, i in ops if i < done and counts[k] == 0]
            rep.add("F8", "Ninguna escritura repetida ni perdida", OK if not dup and not lost else FAIL,
                    f"tarea {task.state.value} ({done}/{steps} pasos); duplicadas: {len(dup)}, perdidas: {len(lost)}",
                    {"task_state": task.state.value, "task_reason": task.reason, "duplicates": dup, "lost": lost,
                     "counts": counts, "lease_history": task.lease.to_dict()["history"]})

        # F9 -----------------------------------------------------------
        if simulate or not open_browser:
            rep.add("F9", "Comportamiento de la sesión del navegador", SKIP, "sin navegador en esta ejecución")
        else:
            r = _http_json(f"{base}/session-report?sid={sid}")
            segs = [dict(s, ip=mask_ip(s["ip"])) for s in r.get("source_ips", [])]
            alive = (r.get("last_beat_age_s") or 999) < 5
            rep.add("F9", "Comportamiento de la sesión del navegador", OK if alive else FAIL,
                    f"hueco máximo {r.get('max_gap_s')} s, recargas {r.get('page_reloads')}, "
                    f"{len(segs)} tramos de IP vista por el servidor; {'sigue latiendo' if alive else 'dejó de latir'}",
                    {"gaps": r.get("gaps"), "source_ips": segs, "page_reloads": r.get("page_reloads"),
                     "nota": "El navegador usa la ruta de Windows, no la del motor: esto mide lo que vive la persona."})
        return _finish(rep, engine, out_dir, routes_before, simulate, engines, verify_file)
    finally:
        engine.shutdown("fin de la validación")


def run_sin_servidor(objetivo: str, out_dir: str, simulate: bool = False, ask: Callable[[str], Any] = input,
                     config_path: Optional[str] = None, engines: dict = ENGINES) -> dict:
    """Variante SIN servidor propio: solo sondeos de conexión TCP (abrir y cerrar, sin enviar datos) a
    ``objetivo`` (host:puerto) por cada enlace. Sirve para F1, F2 (por puerta de enlace), F3, F6, F7 y F10–F12.
    F4, F5, F8 y F9 necesitan un servidor que cuente escrituras y sesiones: quedan omitidos.
    Elige un objetivo que estés autorizado a usar (p. ej. un servicio del que ya eres cliente)."""
    from .audit import verify_file
    from .config import load
    from .engine import Engine
    from .models import Health, Kind

    host, _, port = objetivo.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit("--objetivo debe ser host:puerto, p. ej. github.com:443")
    rep = Report(simulate)
    fast = simulate
    over = {"data_dir": os.path.join(out_dir, "engine-data"),
            "health": {"targets": [objetivo], "interval_s": 0.05 if fast else 2.0,
                       "max_interval_s": 0.2 if fast else 10.0, "timeout_s": 0.5 if fast else 3.0,
                       "fail_threshold": 3, "recover_threshold": 2},
            "lab": {"allowed_hosts": [host], "rotation_enabled": False},   # no se lanza ninguna tarea: solo sondeos
            "discovery": {"refresh_s": 0.2 if fast else 5.0},
            "egress_check_url": None}
    cfg = load(config_path, overrides=over)
    if simulate:
        from .simulator import SimulatedNetwork, three_links
        net = SimulatedNetwork(three_links())
        engine = Engine(cfg, net, net, mode="simulated")
        poll = 0.02
    else:
        from .discovery import default_provider
        from .net import RealNetwork
        net = None
        engine = Engine(cfg, default_provider(), RealNetwork(), mode="real")
        poll = 0.5
    rep.meta.update({"modo": "sin servidor propio (solo sondeos TCP)", "objetivo": objetivo, "platform": sys.platform,
                     "python": sys.version.split()[0], "rotation_enabled": engine.rotation_enabled,
                     "global_routes_modified_by_engine": False})
    routes_before = default_routes() if not simulate else [{"if": "sim", "next_hop": "sim", "metric": 0}]
    rep.add("F1", "Entorno y rutas por defecto antes", OK if (simulate or sys.platform == "win32") else INFO,
            f"{sys.platform}, Python {rep.meta['python']}, rotación {'activa' if engine.rotation_enabled else 'apagada'}",
            {"routes_before": routes_before})
    engine.start()
    try:
        fisicos = lambda: [c for c in engine.connections()                          # noqa: E731
                           if c.usable and c.gateway and c.kind not in (Kind.VPN, Kind.VIRTUAL)]
        _wait(lambda: len({c.gateway for c in fisicos()}) >= 2, 2 if fast else 20, poll)
        conns = fisicos()
        gws = {c.gateway for c in conns}
        two = len(conns) >= 2 and len(gws) >= 2
        rep.add("F2", "Dos enlaces con puertas de enlace distintas (IP pública sin verificar)", OK if two else FAIL,
                f"{len(conns)} enlaces físicos con puerta de enlace, {len(gws)} distintas",
                [{"name": c.name, "kind": c.kind.value, "gateway": mask_ip(c.gateway), "metric": c.route_metric}
                 for c in conns])
        for cid, t in (("F4", "Sesión de navegador abierta"), ("F5", "Tarea larga por el enlace principal"),
                       ("F8", "Ninguna escritura repetida ni perdida"), ("F9", "Comportamiento de la sesión del navegador")):
            rep.add(cid, t, SKIP, "necesita un servidor propio que cuente escrituras y sesiones")
        if not two:
            rep.add("F3/F6/F7", "Salud, pérdida y recuperación", SKIP, "hacen falta dos enlaces con puertas de enlace distintas")
            return _finish(rep, engine, out_dir, routes_before, simulate, engines, verify_file)
        _wait(lambda: all(engine.health.state_of(c.id) != Health.UNKNOWN for c in conns), 2 if fast else 30, poll)
        if not fast:
            time.sleep(10)
        snap = engine.health.snapshot()
        states = {c.name: {k: snap.get(c.id, {}).get(k) for k in ("state", "avg_latency_ms", "availability_pct")} for c in conns}
        healthy = [n for n, st in states.items() if st["state"] in ("healthy", "degraded")]
        rep.add("F3", f"Estado de cada enlace contra {host}:{port}", OK if len(healthy) >= 2 else FAIL,
                ", ".join(f"{n}: {st['state']} {st['avg_latency_ms']} ms" for n, st in states.items()), states)
        primary = min(conns, key=lambda c: (c.route_metric if c.route_metric is not None else 1 << 30, c.name))
        others = [c for c in conns if c.id != primary.id]
        t_cut = time.time()
        if simulate:
            net.set_state(primary.id, "down")
        else:
            ask(f"\n>>> DESCONECTA AHORA «{primary.name}» (p. ej. desconecta el Wi‑Fi desde la barra de tareas) y pulsa Enter… ")
        det = _wait(lambda: engine.health.state_of(primary.id) == Health.DOWN, 5 if fast else 120, poll)
        otros_ok = [c.name for c in others if engine.health.state_of(c.id) in (Health.HEALTHY, Health.DEGRADED)]
        rep.add("F6", "Pérdida real detectada (sin tarea que conmutar)", OK if det is not None and otros_ok else FAIL,
                (f"caída de «{primary.name}» detectada en {det} s; sigue sano: {', '.join(otros_ok) or 'ninguno'}"
                 if det is not None else "no se detectó la caída"),
                {"detect_s": det, "still_healthy": otros_ok})
        if fast:
            time.sleep(0.3)
            net.set_state(primary.id, "up")
        else:
            time.sleep(20)
            ask(f"\n>>> RECONECTA «{primary.name}» y pulsa Enter… ")
        rec = _wait(lambda: engine.health.state_of(primary.id) == Health.HEALTHY, 5 if fast else 240, poll)
        rep.add("F7", "Recuperación detectada", OK if rec is not None else FAIL,
                f"recuperado en {rec} s" if rec is not None else "no se recuperó a tiempo",
                {"recover_s": rec, "outage_s": round(time.time() - t_cut, 1)})
        return _finish(rep, engine, out_dir, routes_before, simulate, engines, verify_file)
    finally:
        engine.shutdown("fin de la validación")


def _finish(rep, engine, out_dir, routes_before, simulate, engines, verify_file) -> dict:
    # F10 --------------------------------------------------------------
    audit_path = os.path.join(engine.cfg["data_dir"], "audit.jsonl")
    ok, bad = verify_file(audit_path)
    kinds = sorted({e["kind"] for e in engine.audit.events(limit=100000)})
    rep.add("F10", "Cadena de auditoría íntegra", OK if ok else FAIL,
            "íntegra" if ok else f"rota en el evento {bad}", {"path": audit_path, "kinds": kinds})
    # F11 --------------------------------------------------------------
    eng = probe_engines(engines)
    up = [n for n, v in eng.items() if v["listening"]]
    rep.add("F11", "Los tres motores responden a la vez", OK if len(up) == len(eng) else INFO,
            f"responden: {', '.join(up) or 'ninguno'} (no se arranca ninguno; ábrelos tú antes si quieres probarlo)", eng)
    # F12 --------------------------------------------------------------
    after = default_routes() if not simulate else routes_before
    same = after == routes_before
    rep.add("F12", "Rutas por defecto después (el motor no las toca)", OK if same else INFO,
            "idénticas" if same else "distintas: revisa si las cambió Windows al desconectar/reconectar",
            {"routes_after": after})
    jp, mp = rep.write(out_dir)
    print(f"\nTipo de evidencia: {rep.evidence_type}\nInforme: {mp}\nDatos: {jp}")
    return rep.to_dict()
