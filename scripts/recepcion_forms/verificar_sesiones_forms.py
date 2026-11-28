"""E9 · Verificación DINÁMICA, desde el lado de Network Engine RC3, de cómo se comporta una integración de Forms.

Arranca un Network Engine RC3 propio en modo SIMULADO (carpeta temporal, 127.0.0.1, puerto 8990 por omisión), crea UNA
credencial «session» con el nombre de la etiqueta del puesto y ejecuta la orden de prueba que indique Ronaldo (la que
abre su sesión de Forms contra el portal de laboratorio). La credencial viaja por un ARCHIVO, nunca por argumentos.

Mientras la orden corre, observa /status/sessions cada 0,25 s y, según las opciones:
- corta y reconecta el enlace simulado (--cortes N);
- reinicia el motor de forma BRUSCA en cuanto ve una operación «en curso» (--reinicio-en-curso).

Al terminar comprueba, sin mirar el código de Forms:
- que registró la etiqueta esperada, con la credencial session creada aquí, y nunca más de una sesión viva por etiqueta;
- que no hubo latidos rechazados por frecuencia (429) ni con credenciales ajenas;
- que, si hubo reinicio con la operación en curso, la marca «revisar a mano» existe y Forms NO volvió a poner «en curso»
  antes de que una persona la resolviera (la operación a medias no se repite sola);
- que la sesión acabó cerrada o caducada, no abandonada «activa».

No toca Forms, no envía formularios y no sale a internet. Es una prueba de laboratorio con red SIMULADA.

Uso:
  python verificar_sesiones_forms.py --etiqueta forms-ventanilla-1 --forms RUTA --salida DIR \
         [--cortes 2] [--reinicio-en-curso] [--env-puerto CAUCE_NE_PUERTO] [--env-token CAUCE_NE_TOKEN_FILE] \
         -- ORDEN DE PRUEBA DE FORMS...
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
CONTRATO = json.loads((Path(__file__).resolve().parent / "contrato_forms_rc3.json").read_text("utf-8"))
WINDOWS = os.name == "nt"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def libre(p: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", p)) != 0


class Motor:
    def __init__(self, base: Path, puerto: int):
        self.base, self.p, self.proc = base, puerto, None
        self.datos = base / "datos"
        self.datos.mkdir(parents=True, exist_ok=True)
        self.cfg = base / "config.json"
        self.cfg.write_text(json.dumps({"data_dir": str(self.datos), "api": {"port": puerto},
                                        "lab": {"rotation_enabled": False}, "sessions": {"max": 8}}), "utf-8")

    def env(self):
        e = {k: v for k, v in os.environ.items() if not k.startswith(("CAUCE_", "MONITOREO_"))}
        e["PYTHONPATH"] = str(RAIZ)
        return e

    def iniciar(self):
        if not libre(self.p):
            raise SystemExit(f"El puerto {self.p} está ocupado: elige otro con --puerto (no se toca nada ajeno).")
        log = open(self.base / "network-engine.log", "a", encoding="utf-8")
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}
        self.proc = subprocess.Popen([sys.executable, "-m", "cauce", "--config", str(self.cfg), "sim", "--port", str(self.p)],
                                     cwd=str(RAIZ), env=self.env(), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, **kw)
        t = time.time()
        while time.time() - t < 40:
            if not libre(self.p) and (self.datos / "api.token").exists():
                try:
                    self.api("GET", "/ping")
                    return
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.3)
        raise SystemExit("Network Engine RC3 no arrancó (ver network-engine.log)")

    def parar(self, brusco=False):
        if self.proc and self.proc.poll() is None:
            if brusco:
                self.proc.kill()
            elif WINDOWS:
                self.proc.terminate()
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        t = time.time()
        while not libre(self.p) and time.time() - t < 15:
            time.sleep(0.2)

    def admin(self) -> str:
        from cauce.secrets_store import read_secret
        return read_secret(str(self.datos / "api.token"))

    def api(self, metodo, ruta, cuerpo=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.p}/api/v1{ruta}", method=metodo,
                                     data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.admin()})
        with _OPENER.open(req, timeout=10) as r:
            return json.loads(r.read() or b"null")

    def credencial(self, nombre: str, archivo: Path) -> bool:
        r = subprocess.run([sys.executable, "-m", "cauce", "--config", str(self.cfg), "token", "create", "--name", nombre,
                            "--scope", "session", "--out", str(archivo)], cwd=str(RAIZ), env=self.env(),
                           capture_output=True, text=True)
        return r.returncode == 0 and archivo.exists()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        raise SystemExit("Falta la orden de prueba de Forms después de «--».")
    i = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--etiqueta", required=True)
    ap.add_argument("--forms", required=True)
    ap.add_argument("--salida", required=True)
    ap.add_argument("--puerto", type=int, default=8990)
    ap.add_argument("--cortes", type=int, default=0)
    ap.add_argument("--reinicio-en-curso", action="store_true")
    ap.add_argument("--env-puerto", default="CAUCE_NE_PUERTO")
    ap.add_argument("--env-token", default="CAUCE_NE_TOKEN_FILE")
    ap.add_argument("--limite", type=float, default=900)
    a = ap.parse_args(argv[:i])
    orden = argv[i + 1:]
    if not re.match(CONTRATO["etiqueta"]["regex"], a.etiqueta):
        raise SystemExit("Etiqueta no válida para el contrato (2–32, minúsculas, dígitos, - o _).")
    out = Path(a.salida)
    out.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="cauce-verif-forms-"))
    m = Motor(base, a.puerto)
    m.iniciar()
    token = base / f"{a.etiqueta}.token"
    if not m.credencial(a.etiqueta, token):
        m.parar()
        raise SystemExit("No se pudo crear la credencial session")
    obs, eventos, parar = [], [], threading.Event()
    estado = {"reinicio": None, "marca": None}

    def observar():
        while not parar.is_set():
            try:
                v = m.api("GET", "/status/sessions")
                activas = [s for s in (v.get("sessions") or v.get("active") or []) if s.get("label") == a.etiqueta]
                obs.append({"t": round(time.time() - t0, 2), "activas": len(activas),
                            "ops": [s.get("operation") for s in activas], "rh": [s.get("requires_human") for s in activas],
                            "duenos": sorted({s.get("owner_name") for s in activas})})
                if a.reinicio_en_curso and estado["reinicio"] is None and any(o == "in_progress" for o in obs[-1]["ops"]):
                    m.parar(brusco=True)
                    m.iniciar()
                    estado["reinicio"] = round(time.time() - t0, 2)
                    eventos.append({"t": estado["reinicio"], "evento": "reinicio brusco con operación en curso"})
                if estado["reinicio"] is not None and estado["marca"] is None:
                    estado["marca"] = next((p for p in m.api("GET", "/sessions/pending").get("pending", [])
                                            if p.get("label") == a.etiqueta), None)
            except Exception as e:  # noqa: BLE001
                eventos.append({"t": round(time.time() - t0, 2), "evento": f"observador: {type(e).__name__}: {e}"})
            parar.wait(0.25)

    def cortar():
        # Hilo aparte: el observador no deja de mirar durante el corte
        parar.wait(10)
        for _ in range(a.cortes):
            if parar.is_set():
                return
            try:
                m.api("POST", "/sim/connections/sim-a/state", {"state": "down"})
                eventos.append({"t": round(time.time() - t0, 2), "evento": "enlace simulado cortado"})
                parar.wait(10)
                m.api("POST", "/sim/connections/sim-a/state", {"state": "up"})
                eventos.append({"t": round(time.time() - t0, 2), "evento": "enlace simulado reconectado"})
            except Exception as e:  # noqa: BLE001
                eventos.append({"t": round(time.time() - t0, 2), "evento": f"corte: {type(e).__name__}: {e}"})
            parar.wait(20)

    t0 = time.time()
    th = threading.Thread(target=observar, daemon=True)
    th.start()
    th_c = threading.Thread(target=cortar, daemon=True)
    th_c.start()
    env = {**os.environ, a.env_puerto: str(a.puerto), a.env_token: str(token)}
    t = time.time()
    try:
        r = subprocess.run(orden, cwd=a.forms, env=env, timeout=a.limite, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        rc, salida = r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        rc, salida = None, "la orden superó el límite de tiempo"
    dur = round(time.time() - t, 1)
    time.sleep(2)
    parar.set()
    th.join(15)

    # Registro de accesos del motor: quién llamó a qué y con qué resultado (no contiene el contenido de las llamadas)
    accesos = []
    f_acc = m.datos / "access.jsonl"
    if f_acc.exists():
        for linea in f_acc.read_text("utf-8", errors="replace").splitlines():
            try:
                accesos.append(json.loads(linea))
            except ValueError:
                pass
    de_forms = [x for x in accesos if x.get("scope") != "admin" and not (x.get("scope") is None and x.get("path") == "/api/v1/ping")]
    ck = []

    def check(nombre, ok, detalle=None):
        ck.append({"comprobacion": nombre, "ok": bool(ok), "detalle": None if ok else detalle})

    check("la orden de prueba de Forms terminó (código 0)", rc == 0, {"codigo": rc, "salida": salida[-1500:]})
    check(f"Forms registró la etiqueta estable «{a.etiqueta}»",
          any(x.get("path") == "/api/v1/sessions" and x.get("method") == "POST" and x.get("status") in (200, 201)
              for x in de_forms))
    check("nunca hubo más de una sesión viva con esa etiqueta", all(x["activas"] <= 1 for x in obs),
          max((x["activas"] for x in obs), default=0))
    ajenos = sorted({(x.get("who"), x.get("scope")) for x in de_forms if (x.get("who"), x.get("scope")) != (a.etiqueta, "session")})
    check("Forms solo usó la credencial «session» de su puesto (ni admin, ni review, ni read)", not ajenos, ajenos)
    permitidas = re.compile(r"^/api/v1/sessions(/bs_[0-9a-f]+(/heartbeat)?)?$")
    fuera = sorted({(x.get("method"), x.get("path")) for x in de_forms if not permitidas.match(str(x.get("path")))})
    check("Forms solo llamó a sus rutas de sesión (no resolvió marcas ni tocó otras rutas)", not fuera, fuera)
    r429 = [x for x in de_forms if x.get("status") == 429]
    check("ningún latido rechazado por frecuencia (429)", not r429, len(r429))
    r401 = [x for x in de_forms if x.get("status") in (401, 403)]
    check("ninguna llamada rechazada por credencial (401/403)", not r401, len(r401))
    if a.reinicio_en_curso:
        check("hubo una operación «en curso» y se reinició el motor durante ella", estado["reinicio"] is not None)
        post_rh = [x for x in obs if estado["reinicio"] is not None and x["t"] > estado["reinicio"] and True in x["rh"]]
        check("tras el reinicio la sesión quedó «revisar a mano» (o hay marca pendiente con su etiqueta)",
              bool(post_rh) or estado["marca"] is not None)
        # El motor restaura la operación «en curso» tras el reinicio: solo cuenta como repetición una TRANSICIÓN nueva
        # de «inactiva» a «en curso» después del reinicio.
        post = [x for x in obs if estado["reinicio"] is not None and x["t"] > estado["reinicio"]]
        repetida = [b for a_, b in zip(post, post[1:]) if "in_progress" in b["ops"] and "in_progress" not in a_["ops"]
                    and a_["activas"]]
        check("Forms NO volvió a poner la operación «en curso» por su cuenta (no repite)", not repetida, repetida[:3])
        try:
            sigue = [p for p in m.api("GET", "/sessions/pending").get("pending", []) if p.get("label") == a.etiqueta]
        except Exception:  # noqa: BLE001
            sigue = []
        ultimas = [x for x in obs if x["activas"]][-4:]
        check("la marca no desapareció mientras la sesión siguió viva (Forms no la quita)",
              bool(sigue) or all(True in x["rh"] for x in ultimas), ultimas)
    check("al terminar no queda una sesión «activa» abandonada", not (obs and obs[-1]["activas"] and
                                                                         obs[-1]["ops"] == ["in_progress"]))
    res = {"etiqueta": a.etiqueta, "network_engine": CONTRATO["candidata"]["network_engine"], "orden": orden,
           "duracion_s": dur, "cortes_pedidos": a.cortes, "reinicio_en_curso": a.reinicio_en_curso, "eventos": eventos,
           "observaciones": len(obs), "linea_de_tiempo": [x for k, x in enumerate(obs) if k == 0 or
                                                          (x["ops"], x["rh"], x["activas"]) != (obs[k-1]["ops"], obs[k-1]["rh"], obs[k-1]["activas"])], "comprobaciones": ck, "ok": all(x["ok"] for x in ck),
           "evidencia": "LABORATORIO con red SIMULADA (no es una prueba física)"}
    (out / "verificacion_sesiones_forms.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), "utf-8")
    try:
        shutil.copy(base / "network-engine.log", out / "network-engine.log")
    except OSError:
        pass
    for nombre in ("access.jsonl", "audit.jsonl"):
        try:
            shutil.copy(m.datos / nombre, out / nombre)
        except OSError:
            pass
    m.parar()
    shutil.rmtree(base, ignore_errors=True)
    for x in ck:
        print(("OK    " if x["ok"] else "FALLA ") + x["comprobacion"])
    print(f"\n{sum(x['ok'] for x in ck)}/{len(ck)} · evidencia: {out}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
