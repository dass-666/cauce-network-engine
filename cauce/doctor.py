"""Diagnóstico de dependencias, permisos y entorno (``cauce doctor``)."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any

OK, WARN, FAIL = "OK", "AVISO", "FALLO"


def _is_admin() -> bool | None:
    try:
        if sys.platform == "win32":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return None


def run_checks(cfg: dict, discovery=None) -> list[dict[str, Any]]:
    res: list[dict[str, Any]] = []

    def add(name, status, detail):
        res.append({"check": name, "status": status, "detail": detail})

    v = sys.version_info
    add("Python >= 3.10", OK if v >= (3, 10) else FAIL, sys.version.split()[0])
    add("Sistema operativo", OK if sys.platform == "win32" else WARN,
        f"{platform.platform()}" + ("" if sys.platform == "win32" else " (objetivo principal: Windows 11)"))
    if sys.platform == "win32":
        build = sys.getwindowsversion().build
        add("Windows 11 (build >= 22000)", OK if build >= 22000 else WARN, f"build {build}")
        ps = shutil.which("powershell.exe")
        add("PowerShell disponible", OK if ps else FAIL, ps or "no encontrado")
        if ps:
            try:
                p = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command",
                                    "if (Get-Command Get-NetAdapter -ErrorAction SilentlyContinue) {'si'} else {'no'}"],
                                   capture_output=True, text=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                has = p.stdout.strip() == "si"
                add("Módulo NetAdapter (Get-NetAdapter)", OK if has else FAIL, p.stdout.strip() or p.stderr.strip()[:200])
            except Exception as e:
                add("Módulo NetAdapter (Get-NetAdapter)", FAIL, repr(e))
    else:
        add("iproute2 (ip -j)", OK if shutil.which("ip") else WARN, shutil.which("ip") or "no encontrado")

    adm = _is_admin()
    if adm is None:
        add("Privilegios", WARN, "no se pudo determinar")
    elif adm:
        add("Privilegios", WARN, "Se ejecuta como administrador. NO es necesario: el motor solo lee la configuración y enlaza sockets. Ejecútalo como usuario normal.")
    else:
        add("Privilegios", OK, "usuario normal (suficiente; el motor no modifica rutas globales)")

    d = cfg["data_dir"]
    try:
        os.makedirs(d, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=d, delete=True):
            pass
        add("Carpeta de datos escribible", OK, d)
    except Exception as e:
        add("Carpeta de datos escribible", FAIL, f"{d}: {e}")

    port = int(cfg["api"]["port"])
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        add(f"Puerto API 127.0.0.1:{port} libre", OK, "libre")
    except OSError as e:
        add(f"Puerto API 127.0.0.1:{port} libre", WARN, f"ocupado ({e}); ¿el motor ya está en marcha?")
    finally:
        s.close()

    if discovery is not None:
        try:
            conns = discovery.discover()
            usable = [c for c in conns if c.usable and not c.is_virtual]
            add("Detección de interfaces", OK if conns else WARN,
                f"{len(conns)} interfaces, {len(usable)} físicas utilizables: " + ", ".join(f"{c.name}({c.kind.value})" for c in usable))
            gws = {c.gateway for c in usable if c.gateway}
            add("Salidas potencialmente independientes", OK if len(gws) >= 2 else WARN,
                f"{len(gws)} gateways distintos" + ("" if len(gws) >= 2 else ": para probar conmutación real hacen falta al menos 2 salidas con gateway propio"))
        except Exception as e:
            add("Detección de interfaces", FAIL, f"{type(e).__name__}: {e}")
    add("Destinos de diagnóstico configurados", OK if cfg["health"]["targets"] else WARN, ", ".join(cfg["health"]["targets"]) or "ninguno")
    return res


def print_report(res: list[dict[str, Any]]) -> int:
    w = max(len(r["check"]) for r in res)
    worst = 0
    for r in res:
        print(f"[{r['status']:<5}] {r['check']:<{w}}  {r['detail']}")
        worst = max(worst, {OK: 0, WARN: 1, FAIL: 2}[r["status"]])
    print()
    print({0: "Diagnóstico correcto.", 1: "Diagnóstico con avisos.", 2: "Diagnóstico con FALLOS."}[worst])
    return 2 if worst == 2 else 0
