"""Línea de comandos: ``python -m cauce <comando>``.

Comandos:
  run          Arranca el motor real + API + panel (127.0.0.1).
  sim          Arranca el motor con el SIMULADOR de 3 conexiones + API + panel.
  scenario     Ejecuta el escenario de 10 pasos y genera un informe.
  scenario-sessions  Simulación de estabilidad y aislamiento de dos sesiones de navegador (sin navegador real).
  discover     Muestra las interfaces detectadas (--raw guarda la salida cruda).
  doctor       Diagnóstico de dependencias, permisos y entorno.
  panel        Abre el panel en el navegador (con el token de sesión).
  status       Estado del motor en marcha.
  stop         Cierre limpio del motor en marcha.
  labserver    Servidor de laboratorio propio en 127.0.0.1:8770.
  verify-audit Comprueba la integridad del registro de auditoría.
  token        Credenciales read (Monitor) o session (una sesión de navegador): create | list | revoke | recover (requiere el motor en marcha).
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import webbrowser

from . import __version__
from .config import ConfigError, load


def _setup_logging(data_dir: str, verbose: bool) -> str:
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, "cauce.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(sh)
    return path


def _serve(cfg: dict, mode: str, open_browser: bool) -> int:
    from .api import CauceAPI
    from .engine import Engine
    from .secrets_store import storage_method, write_secret

    log_path = _setup_logging(cfg["data_dir"], False)
    log = logging.getLogger("cauce")
    sim = None
    if mode == "simulated":
        from .simulator import SimulatedNetwork, three_links
        sim = SimulatedNetwork(three_links())
        discovery, network = sim, sim
    else:
        from .discovery import default_provider
        from .net import RealNetwork
        discovery, network = default_provider(), RealNetwork()

    engine = Engine(cfg, discovery, network, mode=mode)
    engine.audit.subscribe(lambda ev: ev["kind"].startswith("error") and log.error(ev["message"]))
    try:
        api = CauceAPI(engine, cfg["api"]["host"], int(cfg["api"]["port"]))
    except OSError as e:
        log.error("No se pudo abrir 127.0.0.1:%s (%s). ¿Ya hay un motor en marcha? Usa 'cauce status'.", cfg["api"]["port"], e)
        return 3
    if sim is not None:
        api.enable_sim_control(sim)
    token_path = os.path.join(cfg["data_dir"], "api.token")
    info_path = os.path.join(cfg["data_dir"], "api.json")
    for p in (token_path, info_path):   # restos de una ejecución que no se cerró bien: no deben parecer «listo»
        try:
            os.remove(p)
        except OSError:
            pass

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("Señal %s recibida: cierre limpio", signum)
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    engine.start()
    api.start()
    # Los archivos de descubrimiento se escriben cuando la API ya atiende, cada uno de forma atómica, y
    # api.token el último: quien espera a que exista api.token encuentra también api.json con el puerto
    # correcto y una API que responde (antes había una ventana en la que el CLI usaba el puerto por defecto).
    _write_json_atomic(info_path, {"port": api.port, "pid": os.getpid(), "mode": mode, "version": __version__,
                                   "started": time.time(), "token_storage": storage_method()})
    write_secret(token_path, api.token, atomic=True)
    url = f"http://127.0.0.1:{api.port}/?t={api.token}"
    log.info("Cauce %s en modo %s. API: http://127.0.0.1:%s/api/v1  Registro: %s", __version__, mode, api.port, log_path)
    if mode == "simulated":
        log.info("SIMULACIÓN: las conexiones son ficticias; no es una prueba de hardware.")
    print(f"Panel: {url}\n(Ctrl+C o 'cauce stop' para cerrar)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        while not stop.is_set() and not api.shutdown_requested.is_set():
            stop.wait(0.5)
    finally:
        log.info("Cerrando: deteniendo tareas, monitor y API…")
        engine.shutdown("cierre del proceso")
        api.stop()
        for p in (token_path, info_path):
            try:
                os.remove(p)
            except OSError:
                pass
        log.info("Cierre limpio completado.")
    return 0


def _write_json_atomic(path: str, data: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _client(cfg: dict):
    from .client import CauceClient
    return CauceClient.from_local_install(cfg["data_dir"])


def _safe_console() -> None:
    # En Windows la salida redirigida usa cp1252 y fallaría con caracteres como ✓ o ñ.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    ap = argparse.ArgumentParser(prog="cauce", description=f"Cauce Network Engine {__version__}")
    ap.add_argument("--config", help="Archivo de configuración JSON")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "sim"):
        p = sub.add_parser(name)
        p.add_argument("--open", action="store_true", help="Abrir el panel en el navegador")
        p.add_argument("--port", type=int)
    p = sub.add_parser("scenario")
    p.add_argument("--out", default="reports")
    p = sub.add_parser("scenario-sessions")
    p.add_argument("--out", default="reports")
    p = sub.add_parser("discover")
    p.add_argument("--raw", help="Guardar salida cruda de PowerShell en este archivo (Windows)")
    sub.add_parser("doctor")
    sub.add_parser("panel")
    sub.add_parser("status")
    sub.add_parser("stop")
    p = sub.add_parser("labserver")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--host", default="127.0.0.1",
                   help="Dirección de escucha. 0.0.0.0 solo en TU servidor de laboratorio, nunca en el equipo de trabajo")
    p = sub.add_parser("token")
    tsub = p.add_subparsers(dest="tcmd", required=True)
    tp = tsub.add_parser("create")
    tp.add_argument("--name", required=True, help="Sistema consumidor, p. ej. monitor")
    tp.add_argument("--scope", default="read", choices=["read", "session"],
                    help="read: Monitor (solo /status/*). session: una sesión de navegador (solo /sessions)")
    tp.add_argument("--out", help="Guardar el token protegido (DPAPI en Windows) en este archivo en vez de mostrarlo")
    tsub.add_parser("list")
    tsub.add_parser("recover")
    tp = tsub.add_parser("revoke")
    tp.add_argument("token_id")
    p = sub.add_parser("fieldcheck", help="Validación física guiada con dos enlaces reales (ver docs/VALIDACION_FISICA_WINDOWS.md)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--lab-url", help="URL de TU servidor de laboratorio, p. ej. http://servidor-propio:8770")
    g.add_argument("--objetivo", help="Sin servidor propio: host:puerto que estés autorizado a usar; solo se abre y cierra "
                                      "una conexión TCP por enlace (F1, F2, F3, F6, F7, F10–F12)")
    p.add_argument("--out", default=None, help="Carpeta del informe (por defecto reports/fieldcheck-<fecha>)")
    p.add_argument("--steps", type=int, default=240)
    p.add_argument("--simulate", action="store_true", help="Mismo guion con el simulador (no es validación física)")
    p.add_argument("--no-browser", action="store_true", help="No abrir la página de sesión de navegador")
    p.add_argument("--engines", choices=["normal", "lab"], default="normal",
                   help="F11: puertos de las instalaciones normales o del laboratorio integrado")
    p = sub.add_parser("routers", help="Muestra lo que ven los routers configurados (solo lectura)")
    p = sub.add_parser("verify-audit")
    p.add_argument("path", nargs="?")
    a = ap.parse_args(argv)

    overrides = {}
    if getattr(a, "port", None) and a.cmd in ("run", "sim"):
        overrides["api"] = {"port": a.port}
    if a.cmd == "sim":
        overrides.setdefault("lab", {})["allowed_hosts"] = ["lab.sim", "127.0.0.1", "localhost"]
        overrides["health"] = {"targets": ["sim.diag:443"], "interval_s": 2.0}
    try:
        cfg = load(a.config, overrides)
    except (ConfigError, OSError, ValueError) as e:
        print(f"Configuración inválida: {e}", file=sys.stderr)
        return 2

    if a.cmd == "run":
        return _serve(cfg, "real", a.open)
    if a.cmd == "sim":
        return _serve(cfg, "simulated", a.open)
    if a.cmd == "scenario":
        from .scenario import run
        r = run(a.out)
        print(f"\nInforme: {os.path.abspath(a.out)}/scenario-report.md")
        return 0 if r["ok"] else 1
    if a.cmd == "scenario-sessions":
        from .session_scenario import run as run_sessions
        r = run_sessions(a.out)
        print(f"\nInforme: {os.path.abspath(a.out)}/sessions-scenario-report.md")
        return 0 if r["ok"] else 1
    if a.cmd == "discover":
        from .discovery import default_provider
        from .independence import analyze
        prov = default_provider()
        if a.raw and hasattr(prov, "raw"):
            with open(a.raw, "w", encoding="utf-8") as fh:
                json.dump(prov.raw(), fh, indent=2)
            print(f"Salida cruda guardada en {a.raw} (revísala antes de compartirla: contiene MAC e IP locales)")
        conns = analyze(prov.discover())
        for c in conns:
            print(f"{c.id:<10} {c.name[:28]:<28} {c.kind.value:<9} {c.link.value:<9} ip={c.primary_ip or '-':<15} gw={c.gateway or '-':<15} "
                  f"virtual={'sí' if c.is_virtual else 'no'} salida={c.independence.value}  [{c.description}]")
        return 0
    if a.cmd == "doctor":
        from .doctor import print_report, run_checks
        try:
            from .discovery import default_provider
            prov = default_provider()
        except Exception:
            prov = None
        return print_report(run_checks(cfg, prov))
    if a.cmd == "fieldcheck":
        from .fieldcheck import ENGINES, ENGINES_LAB, run as fieldcheck, run_sin_servidor
        out = a.out or os.path.join("reports", "fieldcheck-" + time.strftime("%Y%m%d-%H%M%S"))
        if a.objetivo:
            res = run_sin_servidor(a.objetivo, out, simulate=a.simulate, config_path=a.config,
                                   ask=(lambda _msg: None) if a.simulate else input,
                                   engines=ENGINES_LAB if a.engines == "lab" else ENGINES)
            return 0 if not res["failed"] else 1
        res = fieldcheck(a.lab_url, out, simulate=a.simulate, steps=a.steps, config_path=a.config,
                         step_delay_s=0.02 if a.simulate else 1.0, open_browser=not a.no_browser,
                         ask=(lambda _msg: None) if a.simulate else input,
                         engines=ENGINES_LAB if a.engines == "lab" else ENGINES)
        return 0 if not res["failed"] else 1
    if a.cmd == "routers":
        from .status import routers_view
        from .engine import Engine
        from .simulator import SimulatedNetwork, three_links
        net = SimulatedNetwork(three_links())
        eng = Engine(cfg, net, net, mode="simulated")
        for rw in eng.routers:
            rw.poll(force=True)
        print(json.dumps(routers_view(eng, time.time()), ensure_ascii=False, indent=2))
        if not eng.routers:
            print("Sin routers configurados (clave \"routers\" en config.json; solo transporte simulado).")
        return 0
    if a.cmd == "labserver":
        from .labserver import LabServer
        srv = LabServer(host=a.host, port=a.port).start()
        print(f"Servidor de laboratorio en http://{a.host}:{srv.port} (Ctrl+C para cerrar)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return 0
    if a.cmd == "verify-audit":
        from .audit import verify_file
        path = a.path or os.path.join(cfg["data_dir"], "audit.jsonl")
        ok, seq = verify_file(path)
        print("Registro íntegro" if ok else f"Registro ALTERADO a partir del evento {seq}")
        return 0 if ok else 1
    try:
        c = _client(cfg)
    except OSError:
        print("El motor no está en marcha (no hay token de sesión).", file=sys.stderr)
        return 3
    try:
        c.ping()
    except OSError:
        # api.json y api.token quedaron de una ejecución que no se cerró bien (corte de luz,
        # proceso terminado a la fuerza). Se sustituyen al arrancar de nuevo el motor.
        print("El motor no está en marcha (quedaron datos de una ejecución que no se cerró "
              "correctamente; se reemplazan al volver a arrancarlo).", file=sys.stderr)
        return 3
    if a.cmd == "token":
        from .client import CauceAPIError
        try:
            if a.tcmd == "create":
                rec = c.create_token(a.name, a.scope)
                if a.out:
                    from .secrets_store import write_secret
                    write_secret(a.out, rec["token"])
                    print(f"Credencial {rec['id']} ({rec['name']}, {rec['scope']}) guardada en {a.out}. El token no se muestra.")
                else:
                    print(f"Credencial {rec['id']} ({rec['name']}, {rec['scope']}). Cópiala ahora; no se volverá a mostrar:\n{rec['token']}")
            elif a.tcmd == "recover":
                r = c.recover_tokens()
                print("Almacén verificado; bloqueo levantado." if r["recovered"] else r["note"])
            elif a.tcmd == "list":
                st = c.tokens_status()
                if st["locked"]:
                    print(f"*** ALMACÉN BLOQUEADO ({st['locked']}): las credenciales read no se aceptan. "
                          f"Pendientes: {st['pending_revocations']}. Use 'cauce token recover' ***")
                for t in c.tokens():
                    estado = "REVOCACIÓN PENDIENTE" if t.get("revocation_pending") else ("REVOCADA" if t["revoked_at"] else "activa")
                    print(f"{t['id']}  {t['name']:<20} {t['scope']:<6} {estado:<9} peticiones={t['requests']}")
            else:
                t = c.revoke_token(a.token_id)
                print(f"Credencial {t['id']} ({t['name']}) revocada.")
        except CauceAPIError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "status":
        print(json.dumps(c.diagnostics(), indent=2, ensure_ascii=False))
        return 0
    if a.cmd == "stop":
        c.shutdown()
        print("Cierre solicitado.")
        return 0
    if a.cmd == "panel":
        url = f"http://127.0.0.1:{c.base.rsplit(':', 1)[1]}/?t={c.token}"
        webbrowser.open(url)
        print(url)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
