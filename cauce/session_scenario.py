"""Escenario de ESTABILIDAD y AISLAMIENTO de dos sesiones de navegador, sobre el SIMULADOR.

Ejecuta: ``python -m cauce scenario-sessions --out reports/``

Qué es: la API real del motor (HTTP en 127.0.0.1) con dos credenciales ``session`` que hacen de
"dueño" de cada sesión (lo que hará Cauce Forms) y una credencial ``read`` (lo que hará Monitor).
Las conexiones son ficticias (simulador) y el tiempo es un reloj simulado: 30 minutos simulados con un
latido cada 5 s por sesión se ejecutan en segundos.

Qué NO es: no abre navegadores, no visita sitios, no envía formularios ni reservas, y no prueba
hardware real ni Windows físico. Prueba la LÓGICA de seguimiento de estado y el aislamiento.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import threading
from typing import Any

from . import __version__
from .api import CauceAPI
from .audit import AuditLog
from .client import CauceAPIError, CauceClient, CauceReadClient, CauceSessionClient
from .config import load
from .engine import Engine
from .scenario import Report
from .simulator import SimulatedNetwork, three_links

SIM_MINUTES = 30
BEAT_S = 5.0


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self._l = threading.Lock()

    def __call__(self) -> float:
        with self._l:
            return self.t

    def advance(self, s: float) -> None:
        with self._l:
            self.t += s


def run(out_dir: str | None = None, quiet: bool = False) -> dict[str, Any]:
    data_dir = tempfile.mkdtemp(prefix="cauce-sessions-")
    cfg = load(overrides={"data_dir": data_dir, "health": {"targets": ["sim.diag:443"]},
                          "lab": {"allowed_hosts": ["lab.sim"]}})
    net = SimulatedNetwork(three_links())
    eng = Engine(cfg, net, net, audit=AuditLog(os.path.join(data_dir, "audit.jsonl")), mode="simulated")
    clock = _Clock()
    eng.sessions.clock = clock
    eng.refresh()
    eng.health.check_all(force=True)
    api = CauceAPI(eng, "127.0.0.1", 0)
    api.start()
    rep = Report()
    timeline: list[dict[str, Any]] = []
    try:
        admin = CauceClient(api.token, port=api.port)
        tok_a = admin.create_token("forms-a", "session")
        tok_b = admin.create_token("forms-b", "session")
        read = CauceReadClient(admin.create_token("monitor", "read")["token"], port=api.port)
        A = CauceSessionClient(tok_a["token"], port=api.port)
        B = CauceSessionClient(tok_b["token"], port=api.port)

        def states() -> dict[str, str]:
            return {s["label"]: s["state"] for s in read.sessions()["sessions"]}

        # 1. registro
        s = rep.step(1, "Registro de dos sesiones autorizadas, cada una con su credencial y su conexión")
        sa = A.register("nav-a", connection_id="sim-a")["id"]
        sb = B.register("nav-b", connection_id="sim-b")["id"]
        rep.check(s, "ambas en 'pending' antes del primer latido", states() == {"nav-a": "pending", "nav-b": "pending"}, states())
        try:
            CauceSessionClient(admin.create_token("forms-c", "session")["token"], port=api.port).register("nav-c")
            rep.check(s, "una tercera sesión se rechaza (máximo 2)", False)
        except CauceAPIError as e:
            rep.check(s, "una tercera sesión se rechaza (máximo 2)", e.code == "too_many_sessions", e.code)

        # 2. intento cruzado
        s = rep.step(2, "Aislamiento de credenciales: B intenta actuar sobre A")
        B2 = CauceSessionClient(tok_b["token"], port=api.port)
        B2.session_id = sa
        codes = []
        for fn in (lambda: B2.heartbeat(False, "other"), B2.get, B2.close):
            try:
                fn()
                codes.append("ACEPTADO")
            except CauceAPIError as e:
                codes.append(e.code)
        rep.check(s, "latido, lectura y cierre cruzados devuelven 403 not_session_owner",
                  codes == ["not_session_owner"] * 3, codes)
        rep.check(s, "A no recibió ningún latido ajeno", eng.sessions.get(sa, "", admin=True).heartbeats == 0)

        # 3. bucle de 30 minutos simulados con fallos inyectados
        s = rep.step(3, f"{SIM_MINUTES} min simulados, latido cada {BEAT_S:.0f} s, con fallos inyectados")
        ticks = int(SIM_MINUTES * 60 / BEAT_S)
        plan = {  # tick -> acción (en minutos simulados: tick*5/60)
            24: ("sim-a cae (sin internet)", lambda: net.set_state("sim-a", "down")),
            60: ("sim-a se recupera", lambda: net.set_state("sim-a", "up")),
            120: ("B deja de latir (navegador B colgado)", None),
            150: ("B vuelve a latir", None),
            200: ("A informa error page_unreachable", None),
            204: ("A vuelve a ok", None),
            260: ("sim-b se desconecta (sin enlace)", lambda: net.set_state("sim-b", "disconnected")),
            300: ("sim-b reconecta", lambda: net.set_state("sim-b", "up")),
        }
        b_paused = False
        a_error = False
        errors: list[str] = []
        samples: dict[str, dict[str, int]] = {"nav-a": {}, "nav-b": {}}
        hist: list[tuple[int, dict[str, str]]] = []
        for t in range(ticks):
            clock.advance(BEAT_S)
            if t in plan:
                label, act = plan[t]
                if act:
                    act()
                    eng.refresh()
                b_paused = {120: True, 150: False}.get(t, b_paused)
                a_error = {200: True, 204: False}.get(t, a_error)
                timeline.append({"min": round(t * BEAT_S / 60, 1), "evento": label})
            for c in eng.connections():
                eng.health.check(c)
            try:
                A.heartbeat(not a_error, "page_unreachable" if a_error else None)
                if not b_paused:
                    B.heartbeat()
            except CauceAPIError as e:
                errors.append(f"t={t}: {e}")
            st = states()
            hist.append((t, st))
            for k, v in st.items():
                samples[k][v] = samples[k].get(v, 0) + 1
            if t in plan or t - 1 in plan:
                timeline[-1].setdefault("estados", []).append({"min": round(t * BEAT_S / 60, 1), **st})
        rep.check(s, "ningún latido rechazado durante el bucle", not errors, errors[:5])
        rep.check(s, "muestras por estado (nav-a)", True, samples["nav-a"])
        rep.check(s, "muestras por estado (nav-b)", True, samples["nav-b"])

        evs = [e for e in read.events(0, 500)["events"] if e["kind"] == "session.state"]
        tr = {sid: [(e["old"], e["new"]) for e in evs if e["session_id"] == sid] for sid in (sa, sb)}

        s = rep.step(4, "Caída y recuperación de sim-a: solo afecta a nav-a")
        rep.check(s, "nav-a pasó a network_down y volvió a active", ("network_degraded", "network_down") in tr[sa]
                  and ("network_down", "active") in tr[sa], tr[sa])
        win = {x[1]["nav-b"] for x in hist if 24 <= x[0] < 70}
        rep.check(s, "nav-b siguió active durante toda la caída de sim-a", win == {"active"}, sorted(win))

        s = rep.step(5, "Latidos interrumpidos en B: stale solo en nav-b, recuperación al volver")
        rep.check(s, "nav-b: active -> stale -> active", ("active", "stale") in tr[sb] and ("stale", "active") in tr[sb], tr[sb])
        rep.check(s, "nav-a nunca estuvo stale", "stale" not in samples["nav-a"], samples["nav-a"])
        win = {x[1]["nav-a"] for x in hist if 120 <= x[0] < 160}
        rep.check(s, "nav-a siguió active mientras B no latía", win == {"active"}, sorted(win))

        s = rep.step(6, "Error informado por A: browser_error solo en nav-a")
        rep.check(s, "nav-a: active -> browser_error -> active",
                  ("active", "browser_error") in tr[sa] and ("browser_error", "active") in tr[sa], tr[sa])
        rep.check(s, "nav-b nunca vio browser_error", "browser_error" not in samples["nav-b"], samples["nav-b"])

        s = rep.step(7, "Desconexión del enlace de sim-b: solo afecta a nav-b")
        rep.check(s, "nav-b pasó a network_down y volvió", ("network_down", "active") in tr[sb], tr[sb])
        win_b = {x[1]["nav-b"] for x in hist if 262 <= x[0] < 300}
        rep.check(s, "nav-b estuvo network_down mientras sim-b no tenía enlace", "network_down" in win_b, sorted(win_b))
        win = {x[1]["nav-a"] for x in hist if 260 <= x[0] < 310}
        rep.check(s, "nav-a siguió active durante la desconexión de sim-b", win == {"active"}, sorted(win))

        s = rep.step(8, "Datos mínimos: el latido rechaza URL o datos de página; la vista read no expone IP ni credenciales")
        try:
            api_raw = CauceClient(tok_a["token"], port=api.port)
            api_raw._call("POST", f"/sessions/{sa}/heartbeat", {"status": "ok", "url": "https://ejemplo.invalid/"})
            rep.check(s, "latido con 'url' rechazado", False)
        except CauceAPIError as e:
            rep.check(s, "latido con 'url' rechazado", e.code == "unsupported_field", e.code)
        text = json.dumps(read.sessions()) + json.dumps(read.events(0, 500))
        leaked = [x for x in ("10.10.1.20", "198.51.100.10", "10.20.2.30", "203.0.113.20", tok_a["id"], tok_b["id"]) if x in text]
        rep.check(s, "sin IP ni ids de credencial en /status/sessions ni /status/events", not leaked, leaked)

        s = rep.step(9, "Estado final")
        final = read.sessions()
        rep.check(s, "ambas sesiones active al final", states() == {"nav-a": "active", "nav-b": "active"}, states())
        rep.check(s, "aislamiento declarado", final["isolation"] == {"one_credential_per_session": True, "shared_connection": False},
                  final["isolation"])
        A.close()
        B.close()
        rep.check(s, "cierre limpio de ambas", eng.sessions.all() == [])
    finally:
        api.stop()

    result = {
        "ok": rep.ok, "version": __version__, "mode": "SIMULACIÓN", "python": sys.version.split()[0],
        "platform": platform.platform(), "simulated_minutes": SIM_MINUTES, "heartbeat_s": BEAT_S,
        "timeline": timeline, "steps": rep.steps,
    }
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "sessions-scenario.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        with open(os.path.join(out_dir, "sessions-scenario-report.md"), "w", encoding="utf-8") as fh:
            fh.write(_markdown(result))
    if not quiet:
        for st in rep.steps:
            mark = "PASS" if all(c["ok"] for c in st["checks"]) else "FAIL"
            print(f"[{mark}] {st['n']}. {st['title']}")
        print("RESULTADO:", "PASS" if rep.ok else "FAIL")
    return result


def _markdown(r: dict[str, Any]) -> str:
    L = ["# Escenario de sesiones de navegador (SIMULACIÓN)", "",
         "> Simulación: conexiones ficticias, reloj simulado y API real en 127.0.0.1. **No** se abrió ningún navegador,",
         "> no se visitó ningún sitio y no se envió ningún formulario ni reserva. No prueba hardware ni Windows físico.", "",
         f"- Resultado: **{'PASS' if r['ok'] else 'FAIL'}**",
         f"- Versión {r['version']} · Python {r['python']} · {r['platform']}",
         f"- {r['simulated_minutes']} min simulados, latido cada {r['heartbeat_s']:.0f} s por sesión", "",
         "## Fallos inyectados", ""]
    for t in r["timeline"]:
        L.append(f"- min {t['min']}: {t['evento']}")
    L += ["", "## Comprobaciones", ""]
    for st in r["steps"]:
        L.append(f"### {st['n']}. {st['title']}")
        for c in st["checks"]:
            ev = "" if c["evidence"] is None else f" — `{json.dumps(c['evidence'], ensure_ascii=False)[:300]}`"
            L.append(f"- {'PASS' if c['ok'] else 'FAIL'}: {c['label']}{ev}")
        L.append("")
    return "\n".join(L)
