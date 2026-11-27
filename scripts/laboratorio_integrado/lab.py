"""Laboratorio integrado: Monitor + Forms + Network Engine en una carpeta propia, con pruebas y un informe.

Solo biblioteca estándar. Lo llaman los lanzadores .cmd de esta carpeta (ver docs/LABORATORIO_INTEGRADO_WINDOWS.md).

Garantías (y cómo se cumplen):
- **No toca instalaciones existentes.** Todo vive en la carpeta del laboratorio (por defecto %USERPROFILE%\\cauce-lab-integrado),
  marcada con `.cauce-lab`. Se niega a usar C:\\cauce-msi, la carpeta de Network Engine instalado (%LOCALAPPDATA%\\CauceNetworkEngine),
  una carpeta con `.env` de Monitor o cualquier carpeta no vacía que no haya creado él.
- **Puertos propios** (8100, 8865, 8880, 8870): no choca con Monitor 8000, Forms 8765 ni Network Engine 8780/8765. Si un puerto
  está ocupado no arranca nada y dice quién lo usa; nunca cierra procesos ajenos.
- **Procesos hijos con entorno limpio:** se quitan las variables MONITOREO_*, CAUCE_* y PLAYWRIGHT_* heredadas.
- **Detener** solo termina los procesos que este laboratorio arrancó (PID guardado + comprobación de su línea de órdenes).
- **Red:** en modo `simulado` (por defecto) no sale nada a internet. En modo `real` Network Engine solo observa las interfaces y sondea
  **tu** servidor de laboratorio; no cambia rutas ni adaptadores; la rotación sigue apagada.
- Datos ficticios. Monitor arranca en entorno de desarrollo con su propia base en la carpeta del laboratorio.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
NE_REPO = AQUI.parent.parent
VERSIONES = json.loads((AQUI / "versiones.json").read_text("utf-8"))
MARCA = ".cauce-lab"
WINDOWS = os.name == "nt"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 127.0.0.1 nunca por un proxy


# ------------------------------------------------------------------------------------------------ utilidades
def ahora() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def decir(msg: str) -> None:
    print(msg, flush=True)


def carpeta_por_omision() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "cauce-lab-integrado"


def prohibidas() -> list[Path]:
    out = [Path("C:/cauce-msi")]
    if os.environ.get("LOCALAPPDATA"):
        out.append(Path(os.environ["LOCALAPPDATA"]) / "CauceNetworkEngine")
    return out


def _dentro(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except (ValueError, OSError):
        return False


def validar_carpeta(d: Path, crear: bool) -> None:
    for p in prohibidas():
        if _dentro(d, p) or _dentro(p, d):
            raise SystemExit(f"No se usa {d}: contiene o está dentro de una instalación existente ({p}).")
    if (d / ".env").exists() or (d / "monitor" / ".env").exists():
        raise SystemExit(f"{d} tiene un .env de Monitor: parece una instalación real. Elige otra carpeta.")
    if d.exists() and any(d.iterdir()) and not (d / MARCA).exists():
        raise SystemExit(f"{d} no está vacía y no la creó este laboratorio. Elige otra carpeta.")
    if not d.exists():
        if not crear:
            raise SystemExit(f"No existe {d}. Ejecuta primero la instalación (1_INSTALAR.cmd).")
        d.mkdir(parents=True)
    (d / MARCA).write_text("Carpeta del laboratorio integrado de Cauce. Se puede borrar entera.\n", "utf-8")


def puerto_libre(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", p)) != 0


def quien_usa(p: int) -> str:
    if not WINDOWS:
        return ""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15).stdout
        pids = {ln.split()[-1] for ln in out.splitlines() if f"127.0.0.1:{p} " in ln or f"0.0.0.0:{p} " in ln}
        nombres = []
        for pid in pids:
            t = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                               text=True, timeout=15).stdout.strip()
            nombres.append(f"PID {pid} {t.split(',')[0].strip(chr(34)) if t else ''}".strip())
        return ", ".join(nombres)
    except (OSError, subprocess.SubprocessError):
        return ""


def http(url: str, metodo: str = "GET", cuerpo=None, token: str | None = None, timeout: float = 5.0):
    req = urllib.request.Request(url, method=metodo, data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (OSError, ValueError):
        return None, None


def entorno_limpio(extra: dict[str, str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("MONITOREO_", "CAUCE_", "PLAYWRIGHT_"))}
    env.update(extra)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Lab:
    def __init__(self, d: Path):
        self.d = d
        self.p = VERSIONES["puertos"]
        self.estado_f = d / "estado.json"
        self.estado = json.loads(self.estado_f.read_text("utf-8")) if self.estado_f.exists() else {}

    # -------------------------------------------------------------------- rutas
    @property
    def ne(self) -> Path:
        return self.d / "network-engine"

    @property
    def monitor(self) -> Path:
        return self.d / "monitor"

    @property
    def forms(self) -> Path:
        return self.d / "forms"

    def datos(self, *p) -> Path:
        r = self.d / "datos" / Path(*p)
        r.parent.mkdir(parents=True, exist_ok=True)
        return r

    def guardar(self) -> None:
        self.estado_f.write_text(json.dumps(self.estado, indent=2, ensure_ascii=False), "utf-8")

    def resultado(self, clave: str, datos: dict) -> dict:
        datos = {"cuando": ahora(), **datos}
        self.estado.setdefault("resultados", {})[clave] = datos
        self.guardar()
        return datos

    def py(self, cual: str) -> str:
        """Intérprete de Monitor o Forms (su entorno virtual) o, si se instaló con --sin-venv, el actual."""
        venv = (self.d / cual / ".venv" / ("Scripts" if WINDOWS else "bin") / ("python.exe" if WINDOWS else "python"))
        return str(venv) if venv.exists() else sys.executable

    # -------------------------------------------------------------------- instalación
    def _descargar(self, nombre: str, destino: Path, fuente_local: str | None) -> str:
        v = VERSIONES[nombre]
        if destino.exists():
            shutil.rmtree(destino)
        if fuente_local:
            src = Path(fuente_local)
            commit = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            shutil.copytree(src, destino, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "data", "logs",
                                                                         "__pycache__", "reports", ".browsers"))
            return commit or "copia local"
        url = f"https://codeload.github.com/{v['repo']}/zip/{v['commit']}"
        decir(f"  Descargando {nombre} {v['commit'][:7]} …")
        with urllib.request.urlopen(url, timeout=120) as r:            # repositorio público, commit exacto
            z = zipfile.ZipFile(io.BytesIO(r.read()))
        raiz = z.namelist()[0].split("/")[0]
        tmp = destino.with_name(destino.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        z.extractall(tmp)
        (tmp / raiz).rename(destino)
        shutil.rmtree(tmp, ignore_errors=True)
        return v["commit"]

    def _venv(self, cual: str, requisitos: str) -> None:
        carpeta = self.d / cual
        decir(f"  Entorno de Python para {cual} (puede tardar unos minutos) …")
        subprocess.run([sys.executable, "-m", "venv", str(carpeta / ".venv")], check=True)
        subprocess.run([self.py(cual), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
                        str(carpeta / requisitos)], check=True)

    def instalar(self, sin_venv: bool, sin_forms: bool, navegador_forms: bool, fuentes: dict[str, str]) -> dict:
        decir(f"Instalando el laboratorio en {self.d}")
        ign = shutil.ignore_patterns(".git", "reports", "__pycache__", "*.pyc")
        if self.ne.exists():
            shutil.rmtree(self.ne)
        shutil.copytree(NE_REPO, self.ne, ignore=ign)
        commits = {"network_engine": subprocess.run(["git", "-C", str(NE_REPO), "rev-parse", "HEAD"], capture_output=True,
                                                    text=True).stdout.strip() or "sin git"}
        commits["monitor"] = self._descargar("monitor", self.monitor, fuentes.get("monitor"))
        if not sin_venv:
            self._venv("monitor", "requirements.txt")
        if not sin_forms:
            commits["forms"] = self._descargar("forms", self.forms, fuentes.get("forms"))
            if not sin_venv:
                self._venv("forms", "requirements.txt")
                if navegador_forms:
                    subprocess.run([self.py("forms"), "-m", "playwright", "install", "chromium"], check=False)
        self.estado.update({"instalado": ahora(), "commits": commits, "forms": not sin_forms})
        self.estado.setdefault("monitor_admin", secrets.token_urlsafe(14))      # solo laboratorio, datos ficticios
        self.guardar()
        return self.resultado("instalacion", {"ok": True, "commits": commits})

    # -------------------------------------------------------------------- comprobaciones
    def comprobar_versiones(self) -> dict:
        def leer(p: Path, clave: str) -> str | None:
            try:
                for ln in p.read_text("utf-8").splitlines():
                    if ln.strip().startswith(clave):
                        return ln.split("=", 1)[1].strip().strip("\"'")
            except OSError:
                return None
            return None
        filas = [("network_engine", leer(self.ne / "cauce" / "__init__.py", "__version__")),
                 ("monitor", leer(self.monitor / "monitoreo" / "version.py", "VERSION"))]
        if self.estado.get("forms", True):
            filas.append(("forms", leer(self.forms / "pyproject.toml", "version")))
        det, ok = [], True
        for nombre, encontrada in filas:
            esperada = VERSIONES[nombre]["version"]
            bien = encontrada == esperada
            ok &= bien
            det.append({"componente": nombre, "esperada": esperada, "encontrada": encontrada, "ok": bien,
                        "commit": (self.estado.get("commits") or {}).get(nombre)})
        py_ok = sys.version_info >= (3, 10)
        return self.resultado("versiones", {"ok": ok and py_ok, "python": sys.version.split()[0], "detalle": det})

    def comprobar_puertos(self) -> dict:
        nuestros = set((self.estado.get("pids") or {}).keys())
        det, ok = [], True
        for nombre, p in self.p.items():
            libre = puerto_libre(p)
            mio = nombre in nuestros and not libre
            det.append({"servicio": nombre, "puerto": p, "libre": libre, "del_laboratorio": mio,
                        "usado_por": "" if libre or mio else quien_usa(p)})
            ok &= libre or mio
        otros = {"Monitor normal": 8000, "Forms normal": 8765, "Network Engine 0.2": 8780}
        info = [{"servicio": k, "puerto": v, "en_uso": not puerto_libre(v)} for k, v in otros.items()]
        return self.resultado("puertos", {"ok": ok, "detalle": det, "instalaciones_normales": info})

    # -------------------------------------------------------------------- servicios
    def _cfg_ne(self, modo: str, lab_url: str | None) -> Path:
        cfg = {"data_dir": str(self.datos("network-engine", "x").parent), "api": {"port": self.p["network_engine"]},
               "lab": {"rotation_enabled": False}}
        if modo == "real":
            if not lab_url:
                raise SystemExit("El modo real necesita la URL de TU servidor de laboratorio (--lab-url).")
            from urllib.parse import urlsplit
            u = urlsplit(lab_url)
            cfg["health"] = {"targets": [f"{u.hostname}:{u.port or 80}"]}     # solo tu servidor, nunca terceros
            cfg["lab"]["allowed_hosts"] = [u.hostname, "127.0.0.1", "localhost"]
        f = self.datos("network-engine", "config.json")
        f.write_text(json.dumps(cfg, indent=2), "utf-8")
        return f

    def _lanzar(self, nombre: str, args: list[str], cwd: Path, env: dict[str, str]) -> int:
        log = open(self.datos("logs", f"{nombre}.log"), "a", encoding="utf-8")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if WINDOWS else {"start_new_session": True}
        pr = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
        self.estado.setdefault("pids", {})[nombre] = pr.pid
        self.guardar()
        return pr.pid

    def _esperar_puerto(self, p: int, limite: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < limite:
            if not puerto_libre(p):
                return True
            time.sleep(0.5)
        return False

    def admin_ne(self) -> str | None:
        f = self.d / "datos" / "network-engine" / "api.token"
        if not f.exists():
            return None
        sys.path.insert(0, str(self.ne))
        from cauce.secrets_store import read_secret
        return read_secret(str(f))

    def iniciar_ne(self) -> bool:
        modo = self.estado.get("modo", "simulado")
        cfg = self.datos("network-engine", "config.json")
        args = [sys.executable, "-m", "cauce", "--config", str(cfg), "sim" if modo == "simulado" else "run",
                "--port", str(self.p["network_engine"])]
        self._lanzar("network_engine", args, self.ne, entorno_limpio({}))
        return self._esperar_puerto(self.p["network_engine"], 40)

    def iniciar(self, modo: str, lab_url: str | None, abrir: bool) -> dict:
        self.estado["modo"], self.estado["lab_url"] = modo, lab_url
        self.guardar()
        if modo == "real":
            st, _ = http((lab_url or "").rstrip("/") + "/ok", timeout=8)
            if st != 200:
                return self.resultado("inicio", {"ok": False, "motivo": (
                    f"tu servidor de laboratorio no responde en {lab_url}/ok. Arranca SERVIDOR_LABORATORIO.cmd en el otro "
                    "equipo y comprueba desde el teléfono (solo datos móviles) que esa dirección abre.")})
        pu = self.comprobar_puertos()
        if not pu["ok"]:
            for x in pu["detalle"]:
                if not x["libre"] and not x["del_laboratorio"]:
                    decir(f"  Puerto {x['puerto']} ({x['servicio']}) ocupado {x['usado_por']}")
            return self.resultado("inicio", {"ok": False, "motivo": "puertos ocupados: no se arrancó nada"})
        self._cfg_ne(modo, lab_url)
        det = {}
        decir("  Network Engine …")
        det["network_engine"] = self.iniciar_ne()
        token = self.datos("monitor.token")
        if det["network_engine"] and not token.exists():
            r = subprocess.run([sys.executable, "-m", "cauce", "--config", str(self.datos("network-engine", "config.json")),
                                "token", "create", "--name", "monitor-lab", "--out", str(token)],
                               cwd=str(self.ne), capture_output=True, text=True, env=entorno_limpio({}))
            det["credencial_read"] = r.returncode == 0
        decir("  Monitor …")
        env_m = entorno_limpio({"MONITOREO_ENTORNO": "desarrollo", "MONITOREO_PUERTO": str(self.p["monitor"]),
                                "MONITOREO_DATA_DIR": str(self.datos("monitor", "x").parent),
                                "MONITOREO_ADMIN_PASSWORD": self.estado["monitor_admin"],
                                "MONITOREO_NETWORK_TOKEN_FILE": str(token), "MONITOREO_NETWORK_DIR": str(self.ne),
                                "MONITOREO_NETWORK_PUERTO": str(self.p["network_engine"])})
        self._lanzar("monitor", [self.py("monitor"), "-m", "monitoreo"], self.monitor, env_m)
        det["monitor"] = self._esperar_puerto(self.p["monitor"], 90)
        if self.estado.get("forms", True) and self.forms.exists():
            decir("  Forms …")
            env_f = entorno_limpio({"CAUCE_AUDIT_PATH": str(self.datos("forms", "audit.jsonl")),
                                    "CAUCE_PROFILES_DIR": str(self.datos("forms", "profiles", "x").parent)})
            self._lanzar("forms", [self.py("forms"), "-m", "cauce_form_connector.web.app", "--port", str(self.p["forms"]),
                                   "--no-open"], self.forms, env_f)
            det["forms"] = self._esperar_puerto(self.p["forms"], 60)
        ok = all(det.values())
        if ok and abrir:
            self.abrir()
        return self.resultado("inicio", {"ok": ok, "modo": modo, "servicios": det})

    def abrir(self) -> None:
        import webbrowser
        url = f"http://127.0.0.1:{self.p['monitor']}/#red"
        decir(f"  Monitor: {url}  (usuario admin, contraseña en {self.estado_f})")
        webbrowser.open(url)

    def _cmdline(self, pid: int) -> str:
        if WINDOWS:
            r = subprocess.run(["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                               capture_output=True, text=True)
            return r.stdout
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            return ""

    def detener(self, solo: str | None = None) -> dict:
        hechos = {}
        for nombre, pid in list((self.estado.get("pids") or {}).items()):
            if solo and nombre != solo:
                continue
            cl = self._cmdline(pid)
            marcas = {"network_engine": "cauce", "monitor": "monitoreo", "forms": "cauce_form_connector"}
            if not cl or marcas[nombre] not in cl:
                hechos[nombre] = "ya no estaba en marcha"          # PID reutilizado o terminado: no se toca
            else:
                if WINDOWS:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
                else:
                    import signal
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except OSError:
                        pass
                puerto = self.p[nombre]
                t0 = time.time()
                while not puerto_libre(puerto) and time.time() - t0 < 15:       # espera a que suelte el puerto
                    time.sleep(0.3)
                if not puerto_libre(puerto) and not WINDOWS:
                    try:
                        os.killpg(pid, 9)
                    except OSError:
                        pass
                hechos[nombre] = "detenido" if puerto_libre(puerto) else "no soltó el puerto"
            self.estado["pids"].pop(nombre, None)
        self.guardar()
        return hechos

    # -------------------------------------------------------------------- consultas
    def monitor_token(self) -> str | None:
        st, r = http(f"http://127.0.0.1:{self.p['monitor']}/api/auth/login", "POST",
                     {"username": "admin", "password": self.estado.get("monitor_admin", "")})
        return r.get("token") if st == 200 and isinstance(r, dict) else None

    def red(self, tok: str) -> dict:
        st, r = http(f"http://127.0.0.1:{self.p['monitor']}/api/red", token=tok)
        return r if st == 200 and isinstance(r, dict) else {}

    def esperar_red(self, tok: str, cond, limite: float) -> tuple[bool, float, dict]:
        t0, r = time.time(), {}
        while time.time() - t0 < limite:
            r = self.red(tok)
            if r and cond(r):
                return True, round(time.time() - t0, 1), r
            time.sleep(1.0)
        return False, round(time.time() - t0, 1), r

    def diagnostico(self) -> dict:
        out = {"modo": self.estado.get("modo")}
        adm = self.admin_ne()
        st, d = http(f"http://127.0.0.1:{self.p['network_engine']}/api/v1/diagnostics", token=adm) if adm else (None, None)
        out["network_engine"] = {"responde": st == 200,
                                 **({k: d.get(k) for k in ("version", "mode", "rotation_enabled", "global_routes_modified",
                                                           "connections", "routers")} if isinstance(d, dict) else {})}
        st, c = http(f"http://127.0.0.1:{self.p['network_engine']}/api/v1/status/connectivity", token=adm) if adm else (None, None)
        if isinstance(c, dict):
            out["conectividad"] = {"state": c.get("state"), "reason": c.get("reason"),
                                   "enlaces": [{"tipo": x.get("kind"), "salud": x.get("health"), "latencia_ms": x.get("avg_latency_ms"),
                                                "medida_hace_s": x.get("last_probe_age_s"), "vieja": x.get("stale")}
                                               for x in c.get("connections") or []]}
        tok = self.monitor_token()
        out["monitor"] = {"responde": tok is not None}
        if tok:
            r = self.red(tok)
            out["monitor"].update({"pantalla_red": r.get("network_engine"), "internet": r.get("internet"),
                                   "interfaces": len(r.get("interfaces") or [])})
        if self.estado.get("forms", True):
            out["forms"] = {"responde": not puerto_libre(self.p["forms"])}
        ok = out["network_engine"]["responde"] and out["monitor"].get("pantalla_red") == "disponible"
        return self.resultado("diagnostico", {"ok": bool(ok), **out})

    # -------------------------------------------------------------------- pruebas
    def prueba_ne_apagado(self) -> dict:
        tok = self.monitor_token()
        if not tok:
            return self.resultado("ne_apagado", {"ok": False, "motivo": "Monitor no responde: inicia el laboratorio"})
        pasos = []
        ok0, t, _ = self.esperar_red(tok, lambda r: r.get("network_engine") == "disponible", 60)
        pasos.append({"paso": "Monitor ve Network Engine disponible", "ok": ok0, "s": t})
        self.detener("network_engine")
        ok1, t, r = self.esperar_red(tok, lambda r: r.get("network_engine") in ("no_disponible", "desactualizado"), 90)
        pasos.append({"paso": "Network Engine apagado: Monitor lo indica", "ok": ok1, "s": t, "estado": r.get("network_engine")})
        sanas = [i for i in r.get("interfaces") or [] if i.get("salud") == "SANA"]
        pasos.append({"paso": "Ninguna interfaz se muestra sana sin Network Engine", "ok": not sanas})
        st, _ = http(f"http://127.0.0.1:{self.p['monitor']}/api/info", token=tok)
        pasos.append({"paso": "Monitor sigue respondiendo", "ok": st == 200})
        if self.estado.get("forms", True):
            pasos.append({"paso": "Forms sigue respondiendo", "ok": not puerto_libre(self.p["forms"])})
        self.iniciar_ne()
        ok2, t, r = self.esperar_red(tok, lambda r: r.get("network_engine") == "disponible" and r.get("interfaces"), 150)
        pasos.append({"paso": "Network Engine vuelve: Monitor se reengancha solo", "ok": ok2, "s": t})
        return self.resultado("ne_apagado", {"ok": all(p["ok"] for p in pasos), "evidencia": "LOCAL (sin cortar la red)",
                                             "pasos": pasos})

    def prueba_interrupcion(self, steps: int) -> dict:
        modo = self.estado.get("modo", "simulado")
        if modo == "real":
            out = self.d / "informes" / f"fieldcheck-{dt.datetime.now():%Y%m%d-%H%M%S}"
            decir("  Prueba física guiada contra tu servidor de laboratorio (te pedirá desconectar y reconectar).")
            r = subprocess.run([sys.executable, "-m", "cauce", "fieldcheck", "--lab-url", self.estado["lab_url"],
                                "--out", str(out), "--steps", str(steps), "--engines", "lab"],
                               cwd=str(self.ne), env=entorno_limpio({}))
            js = out / "fieldcheck.json"
            res = json.loads(js.read_text("utf-8")) if js.exists() else {}
            return self.resultado("interrupcion", {"ok": r.returncode == 0, "evidencia": res.get("evidence_type", "FÍSICA"),
                                                   "informe": str(out / "fieldcheck.md"), "fallos": res.get("failed")})
        tok, adm = self.monitor_token(), self.admin_ne()
        if not tok or not adm:
            return self.resultado("interrupcion", {"ok": False, "motivo": "inicia el laboratorio primero"})
        base = f"http://127.0.0.1:{self.p['network_engine']}/api/v1"
        pasos = []
        clave = lambda e: (e.get("ts"), e.get("evento"), e.get("interfaz"))          # noqa: E731
        vistos = {clave(e) for e in self.red(tok).get("eventos") or []}               # solo cuentan los eventos NUEVOS

        def nuevo(tipo):
            return lambda r: any(e.get("tipo") == tipo and clave(e) not in vistos for e in r.get("eventos") or [])
        st, _ = http(base + "/sim/connections/sim-a/state", "POST", {"state": "down"}, adm)
        pasos.append({"paso": "Se corta el enlace simulado principal", "ok": st == 200})
        ok, t, r = self.esperar_red(tok, nuevo("perdida"), 90)
        pasos.append({"paso": "Monitor muestra la pérdida", "ok": ok, "s": t})
        vistos |= {clave(e) for e in r.get("eventos") or []}
        st, _ = http(base + "/sim/connections/sim-a/state", "POST", {"state": "up"}, adm)
        pasos.append({"paso": "Se reconecta el enlace simulado", "ok": st == 200})
        ok, t, r = self.esperar_red(tok, nuevo("recuperada"), 150)
        pasos.append({"paso": "Monitor muestra la recuperación", "ok": ok, "s": t})
        return self.resultado("interrupcion", {"ok": all(p["ok"] for p in pasos),
                                               "evidencia": "SIMULADA (no vale como validación física)", "pasos": pasos})

    # -------------------------------------------------------------------- informe
    def informe(self) -> Path:
        r = self.estado.get("resultados") or {}
        lin = ["# Laboratorio integrado de Cauce: informe", "",
               f"- Generado: {ahora()} · carpeta: `{self.d}` · modo: **{self.estado.get('modo', '—')}**",
               f"- Commits: {json.dumps(self.estado.get('commits') or {}, ensure_ascii=False)}",
               "- Las pruebas en modo **simulado** no valen como validación física. Solo el modo **real** con dos enlaces lo es.",
               "", "| Prueba | Resultado | Cuándo | Evidencia |", "|---|---|---|---|"]
        nombres = {"instalacion": "Instalación", "versiones": "Versiones compatibles", "puertos": "Puertos",
                   "inicio": "Inicio de servicios", "diagnostico": "Diagnóstico de conectividad",
                   "ne_apagado": "Monitor con Network Engine apagado", "interrupcion": "Interrupción y recuperación"}
        for k, n in nombres.items():
            x = r.get(k)
            if x:
                lin.append(f"| {n} | {'PASA' if x.get('ok') else 'FALLA'} | {x.get('cuando')} | {x.get('evidencia', '—')} |")
            else:
                lin.append(f"| {n} | no ejecutada | — | — |")
        lin += ["", "## Detalle", "", "```json", json.dumps(r, indent=2, ensure_ascii=False), "```", ""]
        f = self.d / "informes" / "INFORME_LABORATORIO.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(lin), "utf-8")
        (f.with_suffix(".json")).write_text(json.dumps({"estado": {k: v for k, v in self.estado.items() if k != "monitor_admin"}},
                                                       indent=2, ensure_ascii=False), "utf-8")
        return f


def imprimir(titulo: str, r: dict) -> None:
    decir(f"\n{titulo}: {'PASA' if r.get('ok') else 'FALLA'}")
    for p in r.get("pasos") or r.get("detalle") or []:
        if isinstance(p, dict):
            ok = p.get("ok", p.get("libre"))
            texto = p.get("paso") or p.get("componente") or p.get("servicio")
            extra = {k: v for k, v in p.items() if k not in ("ok", "paso", "componente", "servicio")}
            decir(f"  [{'OK' if ok else 'XX'}] {texto} {extra}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Laboratorio integrado de Cauce (Monitor + Forms + Network Engine)")
    ap.add_argument("--dir", default=str(carpeta_por_omision()), help="Carpeta del laboratorio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("instalar")
    pi.add_argument("--sin-venv", action="store_true", help="usar el Python actual (pruebas automáticas)")
    pi.add_argument("--sin-forms", action="store_true")
    pi.add_argument("--navegador-forms", action="store_true", help="instalar Chromium para Forms (~150 MB)")
    pi.add_argument("--fuente-monitor", help="copiar Monitor de esta carpeta en vez de descargarlo")
    pi.add_argument("--fuente-forms", help="copiar Forms de esta carpeta en vez de descargarlo")
    sub.add_parser("comprobar")
    pn = sub.add_parser("iniciar")
    pn.add_argument("--modo", choices=["simulado", "real"], default="simulado")
    pn.add_argument("--lab-url", help="modo real: URL de TU servidor de laboratorio, p. ej. http://mi-servidor:8770")
    pn.add_argument("--no-abrir", action="store_true")
    sub.add_parser("abrir")
    sub.add_parser("diagnostico")
    sub.add_parser("prueba-ne-apagado")
    pr = sub.add_parser("prueba-interrupcion")
    pr.add_argument("--steps", type=int, default=240)
    sub.add_parser("informe")
    sub.add_parser("detener")
    pt = sub.add_parser("todo", help="comprobar, iniciar, diagnóstico, pruebas e informe (deja los servicios en marcha)")
    pt.add_argument("--modo", choices=["simulado", "real"], default="simulado")
    pt.add_argument("--lab-url")
    pt.add_argument("--no-abrir", action="store_true")
    a = ap.parse_args(argv)

    lab = Lab(Path(a.dir))
    validar_carpeta(lab.d, crear=a.cmd == "instalar")
    if a.cmd == "instalar":
        imprimir("Instalación", lab.instalar(a.sin_venv, a.sin_forms, a.navegador_forms,
                                             {"monitor": a.fuente_monitor, "forms": a.fuente_forms}))
        imprimir("Versiones", lab.comprobar_versiones())
        return 0
    if a.cmd == "comprobar":
        v, p = lab.comprobar_versiones(), lab.comprobar_puertos()
        imprimir("Versiones", v)
        imprimir("Puertos del laboratorio", p)
        return 0 if v["ok"] and p["ok"] else 1
    if a.cmd in ("iniciar", "todo"):
        r = lab.iniciar(a.modo, a.lab_url, not a.no_abrir)
        imprimir("Inicio", {**r, "detalle": [{"servicio": k, "ok": v} for k, v in (r.get("servicios") or {}).items()]})
        if a.cmd == "iniciar" or not r["ok"]:
            return 0 if r["ok"] else 1
        imprimir("Versiones", lab.comprobar_versiones())
        time.sleep(3)
        imprimir("Diagnóstico", lab.diagnostico())
        imprimir("Monitor con Network Engine apagado", lab.prueba_ne_apagado())
        imprimir("Interrupción y recuperación", lab.prueba_interrupcion(240))
        decir(f"\nInforme: {lab.informe()}")
        return 0 if all(x.get("ok") for x in lab.estado["resultados"].values()) else 1
    if a.cmd == "abrir":
        lab.abrir()
        return 0
    if a.cmd == "diagnostico":
        r = lab.diagnostico()
        decir(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r["ok"] else 1
    if a.cmd == "prueba-ne-apagado":
        r = lab.prueba_ne_apagado()
        imprimir("Monitor con Network Engine apagado", r)
        return 0 if r["ok"] else 1
    if a.cmd == "prueba-interrupcion":
        r = lab.prueba_interrupcion(a.steps)
        imprimir("Interrupción y recuperación", r)
        return 0 if r["ok"] else 1
    if a.cmd == "informe":
        decir(f"Informe: {lab.informe()}")
        return 0
    if a.cmd == "detener":
        decir(json.dumps(lab.detener(), ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
