"""Escenario reproducible de prueba funcional (10 pasos) sobre el SIMULADOR.

Ejecuta: ``python -m cauce scenario --out reports/``

Es determinista: no usa hilos de fondo ni temporizadores; los "ticks" del
monitor de salud se invocan explícitamente. Cada paso registra comprobaciones
con resultado PASS/FAIL y la evidencia usada. El informe deja claro que es una
simulación y no una prueba de hardware.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
from typing import Any

from . import __version__
from .audit import AuditLog, verify_chain
from .config import load
from .engine import Engine
from .models import Health, LinkState, TaskState
from .simulator import SimulatedNetwork, default_service, three_links


class Report:
    def __init__(self):
        self.steps: list[dict[str, Any]] = []

    def step(self, n: int, title: str):
        s = {"n": n, "title": title, "checks": []}
        self.steps.append(s)
        return s

    def check(self, s: dict, label: str, ok: bool, evidence: Any = None):
        s["checks"].append({"label": label, "ok": bool(ok), "evidence": evidence})

    @property
    def ok(self) -> bool:
        return all(c["ok"] for s in self.steps for c in s["checks"])


def run(out_dir: str | None = None, quiet: bool = False) -> dict[str, Any]:
    data_dir = tempfile.mkdtemp(prefix="cauce-scenario-")
    cfg = load(overrides={"data_dir": data_dir, "health": {"targets": ["sim.diag:443"]},
                          "lab": {"allowed_hosts": ["lab.sim"]}})
    links = three_links()
    script: dict[int, Any] = {}
    counter = {"n": 0}

    def service(link, req):
        counter["n"] += 1
        status = default_service(link, req)
        action = script.get(counter["n"])
        if action:
            action()
        return status

    net = SimulatedNetwork(links, service)
    audit = AuditLog(os.path.join(data_dir, "audit.jsonl"))
    eng = Engine(cfg, net, net, audit=audit, mode="simulated")
    eng.refresh()
    eng.health.check_all(force=True)
    rep = Report()
    A, B, C = "sim-a", "sim-b", "sim-c"
    hs = lambda cid: eng.health.state_of(cid)

    # 1-3. Inventario inicial
    s = rep.step(1, "La primera conexión funciona")
    a = eng.connection(A)
    rep.check(s, "A detectada con enlace activo e IP", a and a.link == LinkState.UP and a.primary_ip == "10.10.1.20", a and a.to_dict())
    rep.check(s, "Salud de A = healthy", hs(A) == Health.HEALTHY, hs(A).value)
    s = rep.step(2, "La segunda está disponible como alternativa")
    rep.check(s, "Salud de B = healthy", hs(B) == Health.HEALTHY, hs(B).value)
    rep.check(s, "A y B tienen salidas independientes (gateway e IP pública distintos)",
              eng.connection(A).egress_group != eng.connection(B).egress_group,
              {A: eng.connection(A).independence.value, B: eng.connection(B).independence.value})
    s = rep.step(3, "La tercera está desconectada")
    c = eng.connection(C)
    rep.check(s, "C sin enlace y no utilizable", c.link == LinkState.DOWN and not c.usable, c.to_dict())
    rep.check(s, "C no disponible para rutas", not eng.routes.is_available(C), hs(C).value)
    try:
        eng.routes.acquire("prueba", "pedir C", C)
        rep.check(s, "Pedir C explícitamente es rechazado", False)
    except Exception as e:
        rep.check(s, "Pedir C explícitamente es rechazado (sin sustitución silenciosa)", getattr(e, "code", "") == "connection_unavailable", str(e))

    # 4. Selección del operador
    s = rep.step(4, "El operador selecciona una ruta")
    eng.routes.select(A, actor="operador-escenario")
    rep.check(s, "Ruta preferida = A", eng.routes.operator_choice == A, eng.routes.operator_choice)

    # 5-8: una tarea de 6 pasos; el guion cambia la red durante la ejecución
    def lose_a():
        net.set_state(A, "down")

    def a_returns():
        net.set_state(A, "up")
        for _ in range(cfg["health"]["recover_threshold"]):
            eng.health.check(eng.connection(A))  # ticks del monitor

    script[2] = lose_a      # tras la 2ª petición, A pierde conectividad
    script[4] = a_returns   # tras la 4ª, A vuelve
    steps = [{"method": "GET", "url": f"http://lab.sim/ok?i={i}"} for i in range(6)]
    task = eng.start_task({"name": "tarea-lab-1", "owner": "escenario", "steps": steps}, run_async=False)
    res = [r for r in task.results if r["outcome"] == "response"]

    s = rep.step(5, "El motor confirma que la ruta seleccionada se utiliza")
    first = res[:2]
    rep.check(s, "Los 2 primeros pasos salieron por A", all(r["carried_by"] == A for r in first), first)
    rep.check(s, "IP local y IP observada por el servidor coinciden con A", all(r["route_verified"] and r["observed_ip"] == "198.51.100.10" for r in first))

    s = rep.step(6, "La primera pierde conectividad")
    down_ev = [e for e in audit.events(kind="health.change") if e["data"]["connection_id"] == A and e["data"]["new"] == "down"]
    rep.check(s, "Caída de A detectada tras umbral de fallos (no por un único error)", bool(down_ev), down_ev[:1])
    cf = [r for r in task.results if r["outcome"] == "connect_failed"]
    rep.check(s, "El intento por A falló SIN enviar la petición", len(cf) >= 1 and cf[0]["connection_id"] == A, cf[:1])

    s = rep.step(7, "El motor conmuta a la segunda")
    fo = audit.events(kind="route.failover")
    rep.check(s, "Evento de conmutación A -> B con motivo", any(e["data"]["from_connection"] == A and e["data"]["to_connection"] == B and e["data"]["reason"] for e in fo), fo[:1])
    on_b = [r for r in res if r["carried_by"] == B]
    rep.check(s, "Pasos posteriores salieron por B, verificados", bool(on_b) and all(r["route_verified"] for r in on_b), on_b)
    rep.check(s, "Estado de la tarea conservado: ningún paso repetido ni saltado",
              sorted({r['step'] for r in res}) == list(range(6)) and len(res) == 6, [r["step"] for r in res])

    s = rep.step(8, "La primera vuelve")
    rec = audit.events(kind="health.recovered")
    fb = audit.events(kind="route.failback")
    rep.check(s, "Recuperación de A registrada (tras umbral de éxitos)", any(e["data"]["connection_id"] == A for e in rec), rec[:1])
    rep.check(s, "La tarea volvió a A (failback) en un paso posterior", bool(fb) and res[-1]["carried_by"] == A, {"failback": fb[:1], "ultimo_paso": res[-1]})
    rep.check(s, "Tarea completada", task.state == TaskState.COMPLETED, task.state.value)

    # 10. Limitación
    s10 = rep.step(10, "Una respuesta de limitación detiene la operación sin usar otra conexión")
    pol = eng.rotation.create("lab-rr", [A, B], interval_s=10, max_changes_per_hour=10)
    eng.rotation.set_state("lab-rr", "active", "escenario: rotación activa antes de la limitación")
    traffic_before = len(net.traffic)
    t2 = eng.start_task({"name": "tarea-limitada", "owner": "escenario", "connection_id": A, "rotation_policy": "lab-rr",
                         "steps": [{"method": "GET", "url": "http://lab.sim/limit"}, {"method": "GET", "url": "http://lab.sim/ok"}]},
                        run_async=False)
    after = net.traffic[traffic_before:]
    rep.check(s10, "Tarea en estado throttled", t2.state == TaskState.THROTTLED, t2.reason)
    rep.check(s10, "Solo 1 petición enviada, por A; ninguna por B", len(after) == 1 and after[0].connection_id == A,
              [vars(x) for x in after])
    rep.check(s10, "Sin conmutación para esa tarea", all(h.get("kind") != "failover" for h in t2.lease.to_dict()["history"]), t2.lease.to_dict()["history"])
    rep.check(s10, "Segundo paso no ejecutado", t2.current_step == 0, t2.current_step)
    rep.check(s10, "La política de rotación se pausó automáticamente (no rota para sortear el 429)",
              pol.state == "paused", {"state": pol.state, "reason": pol.pause_reason})
    # La política no se puede usar después para sortear: el lease quedó liberado y desvinculado
    rep.check(s10, "Lease liberado y desvinculado de la rotación", t2.lease.released and t2.lease.id not in pol.lease_ids)

    # 9. Registro
    s = rep.step(9, "El sistema registra todos los cambios")
    kinds = {e["kind"] for e in audit.events(limit=10000)}
    needed = {"inventory.added", "route.selected", "route.lease", "health.change", "route.failover",
              "health.recovered", "route.failback", "route.verified", "task.completed", "task.throttled"}
    rep.check(s, "Eventos presentes: " + ", ".join(sorted(needed)), needed <= kinds, sorted(needed - kinds))
    with open(os.path.join(data_dir, "audit.jsonl"), encoding="utf-8") as fh:
        file_events = [json.loads(l) for l in fh if l.strip()]
    ok, broken = verify_chain(file_events)
    rep.check(s, "Cadena de hashes del archivo de auditoría íntegra", ok, {"eventos": len(file_events), "roto_en": broken})
    rep.steps.sort(key=lambda x: x["n"])

    result = {
        "title": "Cauce Network Engine — escenario funcional de 10 pasos",
        "warning": "SIMULACIÓN: valida la lógica del motor, NO el hardware, controladores ni el enrutamiento real de Windows.",
        "version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
        "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ok": rep.ok, "steps": rep.steps,
        "audit_events": file_events,
    }
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "scenario-report.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
        with open(os.path.join(out_dir, "scenario-report.md"), "w", encoding="utf-8") as fh:
            fh.write(to_markdown(result))
    if not quiet:
        print(to_text(result))
    return result


def to_text(r: dict) -> str:
    lines = [r["title"], r["warning"], f"Python {r['python']} · {r['platform']}", ""]
    for s in r["steps"]:
        mark = "PASS" if all(c["ok"] for c in s["checks"]) else "FAIL"
        lines.append(f"[{mark}] {s['n']:>2}. {s['title']}")
        for c in s["checks"]:
            lines.append(f"        {'✓' if c['ok'] else '✗'} {c['label']}")
    lines.append("")
    lines.append("RESULTADO: " + ("TODOS LOS PASOS OK" if r["ok"] else "HAY FALLOS"))
    return "\n".join(lines)


def to_markdown(r: dict) -> str:
    out = [f"# {r['title']}", "", f"> {r['warning']}", "",
           f"- Versión: {r['version']} · Python {r['python']} · {r['platform']}",
           f"- Ejecutado: {r['executed_at']}", f"- Resultado global: **{'OK' if r['ok'] else 'FALLOS'}**", ""]
    for s in r["steps"]:
        mark = "✅" if all(c["ok"] for c in s["checks"]) else "❌"
        out.append(f"## {mark} Paso {s['n']}. {s['title']}")
        for c in s["checks"]:
            out.append(f"- {'✓' if c['ok'] else '✗'} {c['label']}")
        out.append("")
    out.append("## Registro de auditoría (resumen)")
    out.append("")
    out.append("| # | tipo | mensaje |")
    out.append("|---|------|---------|")
    for e in r["audit_events"]:
        out.append(f"| {e['seq']} | `{e['kind']}` | {e['message'].replace('|', '/')} |")
    return "\n".join(out) + "\n"
