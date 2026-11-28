"""E10 · Jornadas SIMULADAS integrales: Network Engine E10 + Monitor E10 + Forms NATIVO (e55a45d) + portal propio.

Todo en 127.0.0.1, red SIMULADA del motor, datos FICTICIOS, portal de laboratorio PROPIO. La «persona» que escribe
ENVIAR es este guion (confirmación SIMULADA). Nada de esto es una prueba física.

Reutiliza (solo lectura, por importación) el laboratorio de jornadas de Forms (``scripts/jornada_laboratorio.py``) y
le añade: Monitor real leyendo el motor (credenciales read y review), el portal E10 (``portal_e10.py``: campos
recibidos para detectar mezclas y modo sin token), y escenarios nuevos: acuse perdido, motor reiniciado durante el
envío, Monitor reiniciado, reinicio simultáneo de los tres, credencial revocada durante el envío y frase incorrecta.

Uso (con el Python de Forms, que tiene Playwright; psutil solo para medir):
  python jornada_e10.py --forms RUTA_FORMS --monitor RUTA_MONITOR --monitor-python PY --expedientes 6 10 20 50 --salida DIR
  python jornada_e10.py ... --duracion-min 60          (prolongada: jornadas de 10 seguidas)
  python jornada_e10.py ... --auditoria                (escenarios que DEMUESTRAN defectos de Forms; ver informe E10)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

AQUI = Path(__file__).resolve().parent
NE_REPO = AQUI.parents[1]


def cargar_jl(forms: Path):
    spec = importlib.util.spec_from_file_location("jornada_laboratorio_forms", forms / "scripts" / "jornada_laboratorio.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    if os.name == "nt":
        # El guion de Forms usa os.killpg y signal.SIGKILL, que no existen en Windows (AttributeError, no OSError):
        # sus jornadas solo corrían en Linux. Aquí se sustituye por taskkill /T (árbol completo: Chromium incluido).
        def matar(self) -> None:
            if self.p and self.p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T", "/F"], capture_output=True)
                self.p.wait(15)

        def parar(self) -> None:
            if self.p and self.p.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.p.pid), "/T"], capture_output=True)
                try:
                    self.p.wait(10)
                except subprocess.TimeoutExpired:
                    matar(self)
        m.Proc.matar, m.Proc.parar = matar, parar
    return m


def construir(JL, forms: Path, monitor: Path, monitor_py: str, salida: Path):
    class LabE10(JL.Laboratorio):
        def __init__(self):
            super().__init__(salida, str(NE_REPO), python=sys.executable)
            env_base = {k: v for k, v in os.environ.items() if not k.startswith(("CAUCE_", "MONITOREO_"))}
            self.portal.args = [sys.executable, str(AQUI / "portal_e10.py"), "--forms", str(forms), "--port",
                                str(self.p_portal), "--capacidad", "100000", "--por-minuto", "1000"]
            self.p_monitor = JL.libre()
            self.dir_mon = salida / "monitor"
            self.dir_mon.mkdir(parents=True, exist_ok=True)
            self.mon_pwd = "lab-" + os.urandom(6).hex()
            self.tok_read = self.dir_ne / "monitor-read.token"
            self.tok_review = self.dir_ne / "monitor-review.token"
            env_m = dict(env_base, MONITOREO_ENTORNO="desarrollo", MONITOREO_PUERTO=str(self.p_monitor),
                         MONITOREO_DATA_DIR=str(self.dir_mon), MONITOREO_ADMIN_PASSWORD=self.mon_pwd,
                         MONITOREO_NETWORK_TOKEN_FILE=str(self.tok_read),
                         MONITOREO_NETWORK_REVISIONES_TOKEN_FILE=str(self.tok_review),
                         MONITOREO_NETWORK_DIR=str(NE_REPO), MONITOREO_NETWORK_PUERTO=str(self.p_ne),
                         PYTHONIOENCODING="utf-8")
            self.monitor = JL.Proc("monitor", [monitor_py, "-m", "monitoreo"], monitor, env_m, salida / "monitor.log",
                                   self.p_monitor)
            self.procs["monitor"] = self.monitor
            self._mtok = None

        def cli_token(self, nombre: str, ambito: str, archivo: Path) -> None:
            if archivo.exists():
                archivo.unlink()
            r = subprocess.run([self.py, "-m", "cauce", "--config", str(self.cfg_ne), "token", "create", "--name", nombre,
                                "--scope", ambito, "--out", str(archivo)], cwd=str(NE_REPO), capture_output=True, text=True)
            assert r.returncode == 0, r.stderr

        def iniciar(self) -> None:
            super().iniciar()
            self.cli_token("monitor-lab-e10", "read", self.tok_read)
            self.cli_token("monitor-lab-e10-revisiones", "review", self.tok_review)
            self.monitor.iniciar()
            self.ev("monitor", puerto=self.p_monitor)

        def detener(self) -> None:
            self.monitor.parar()
            super().detener()

        # --- Monitor (lo que ve el operador)
        def mon(self) -> dict:
            for _ in range(2):
                if not self._mtok:
                    st, r, _ = JL.http("POST", f"http://127.0.0.1:{self.p_monitor}/api/auth/login",
                                       {"username": "admin", "password": self.mon_pwd}, timeout=10)
                    self._mtok = (r or {}).get("token") if st == 200 else None
                st, r, _ = JL.http("GET", f"http://127.0.0.1:{self.p_monitor}/api/red", token=self._mtok, timeout=10)
                if st == 200 and isinstance(r, dict):
                    return r
                self._mtok = None
            return {}

        def revisiones_monitor(self) -> dict:
            s = (self.mon().get("sesiones") or {})
            return {k: s.get(k) for k in ("revisiones_pendientes", "revisiones_red", "revisiones_forms",
                                          "forms_sin_informar", "requieren_persona")} | \
                   {"forms_requiere_persona": any(x.get("requiere_persona") for x in s.get("activas") or []
                                                  if (x.get("etiqueta") or "").startswith("forms-"))}

        def reiniciar_monitor(self) -> None:
            self.monitor.matar()
            self.monitor.iniciar()
            self._mtok = None

        def reiniciar_ne(self) -> None:
            self.ne.matar()
            self.ne.iniciar()

        def revocar_forms(self) -> None:
            _, toks, _ = self.ne_api("GET", "/tokens")
            for t in (toks or {}).get("tokens") or []:
                if t.get("name") == "forms-lab-jornada" and not t.get("revoked"):
                    self.ne_api("DELETE", f"/tokens/{t['id']}")

        def recrear_forms(self) -> None:
            self.cli_token("forms-lab-jornada", "session", self.token_file)

        def recibidos_e10(self) -> list[dict]:
            return JL.http("GET", self.pu + "/_lab/e10/recibidos", timeout=10)[1]["recibidos"]

    return LabE10()


def plan(n: int) -> dict[int, str]:
    tipos = ["cortar_despues", "acuse_perdido", "cortar_antes", "portal_lento", "ne_apagado", "ne_reiniciado",
             "red_caida", "doble_clic", "forms_reiniciado", "monitor_reiniciado", "reinicio_simultaneo",
             "credencial_revocada", "capacidad", "navegador_cerrado", "frase_incorrecta"]
    if n <= 6:
        return {1: "acuse_perdido", 2: "cortar_antes", 3: "reinicio_simultaneo", 4: "credencial_revocada",
                5: "frase_incorrecta"}
    if n <= 10:
        return {1: "cortar_despues", 2: "acuse_perdido", 3: "cortar_antes", 4: "ne_reiniciado", 5: "doble_clic",
                6: "monitor_reiniciado", 7: "reinicio_simultaneo", 8: "credencial_revocada", 9: "frase_incorrecta"}
    paso = max(1, (n - 1) // len(tipos))
    return {1 + k * paso: t for k, t in enumerate(tipos) if 1 + k * paso < n}


def enviar_en_hilo(lab) -> threading.Thread:
    h = threading.Thread(target=lambda: lab.enviar(), daemon=True)
    h.start()
    return h


def esperar_post(lab, pp: int, limite: float = 15) -> None:
    t0 = time.time()
    while lab.portal_api("GET", "/_lab/estadisticas")["peticiones_post"] == pp and time.time() - t0 < limite:
        time.sleep(0.1)


def jornada(JL, lab, n: int, desde: int, etiqueta: str) -> dict:
    filas = JL.filas_ficticias(desde, n)
    lab.cargar_datos(filas)
    fallos = plan(n)
    st0 = lab.portal_api("GET", "/_lab/estadisticas")
    rec0 = len(lab.recibidos_e10())
    res, bloqueos, monitor_vio = [], 0, []
    inciertas_vistas: dict[str, str] = {}
    for i in range(n):
        fallo = fallos.get(i)
        campos = lab.analizar()
        rep = lab.rellenar(campos, i)
        if not rep["ok"]:
            res.append({"i": i, "estado": "relleno_con_incidencias", "fallo": fallo})
            continue
        pp0 = lab.portal_api("GET", "/_lab/estadisticas")["peticiones_post"]
        st, r, extra = 0, None, {}
        if fallo in ("cortar_despues", "cortar_antes"):
            lab.portal_api("POST", "/_lab/fallo", {"tipo": fallo, "veces": 6})
        if fallo == "acuse_perdido":                    # el portal registra, la respuesta se pierde y el acuse tarda
            lab.portal_api("POST", "/_lab/fallo", {"tipo": "cortar_despues", "veces": 6})
            lab.portal_api("POST", "/_lab/fallo", {"tipo": "acuse_lento", "segundos": 20})
        if fallo == "portal_lento":
            lab.portal_api("POST", "/_lab/fallo", {"tipo": "lento", "segundos": 25})
        if fallo == "ne_apagado":
            lab.ne.parar()
        if fallo == "red_caida":
            lab.ne_api("POST", "/sim/connections/sim-a/state", {"state": "down"})
            time.sleep(8)
        if fallo == "capacidad":
            lab.portal_api("POST", "/_lab/limites", {"capacidad": lab.portal_api("GET", "/_lab/estadisticas")["recibidos"]})
        if fallo == "frase_incorrecta":
            s1, r1, _ = JL.http("POST", lab.fu + "/api/submit", {"phrase": "enviar"})
            extra["frase_incorrecta_http"] = s1
            extra["post_tras_frase_incorrecta"] = lab.portal_api("GET", "/_lab/estadisticas")["peticiones_post"] - pp0
            st, r = lab.enviar()                      # la persona escribe bien ENVIAR (simulado)
        elif fallo == "doble_clic":
            lab.portal_api("POST", "/_lab/fallo", {"tipo": "lento", "segundos": 2})
            out: list = []
            hs = [threading.Thread(target=lambda: out.append(lab.enviar())) for _ in range(2)]
            [h.start() for h in hs]
            [h.join() for h in hs]
            ok = [x for x in out if x[0] == 200]
            extra["doble_clic_aceptados"] = len(ok)
            st, r = ok[0] if ok else out[0]
            bloqueos += len(out) - len(ok)
        elif fallo in ("forms_reiniciado", "ne_reiniciado", "monitor_reiniciado", "reinicio_simultaneo",
                       "credencial_revocada", "navegador_cerrado"):
            lab.portal_api("POST", "/_lab/fallo", {"tipo": "lento", "segundos": 6})
            pp = lab.portal_api("GET", "/_lab/estadisticas")["peticiones_post"]
            h = enviar_en_hilo(lab)
            esperar_post(lab, pp)
            time.sleep(0.5)
            if fallo == "forms_reiniciado":
                lab.reiniciar_forms(matar=True)
            elif fallo == "ne_reiniciado":
                lab.reiniciar_ne()
            elif fallo == "monitor_reiniciado":
                lab.reiniciar_monitor()
            elif fallo == "reinicio_simultaneo":
                for p in (lab.forms, lab.ne, lab.monitor):
                    p.matar()
                lab.ne.iniciar()
                lab.forms.iniciar()
                lab.monitor.iniciar()
                lab._mtok = None
            elif fallo == "credencial_revocada":
                lab.revocar_forms()
                h.join(60)
                extra["diag_con_credencial_revocada"] = lab.diag().get("motor")
                lab.recrear_forms()                       # acción del ADMINISTRADOR
                lab.reiniciar_forms(matar=False)
            elif fallo == "navegador_cerrado":
                for x in lab.forms.arbol()[1:]:
                    try:
                        if "chrom" in x.name().lower() or "headless_shell" in x.name().lower():
                            x.kill()
                    except Exception:  # noqa: BLE001
                        pass
            h.join(60)
            if fallo in ("forms_reiniciado", "reinicio_simultaneo", "credencial_revocada", "navegador_cerrado"):
                if fallo == "navegador_cerrado":
                    JL.http("POST", lab.fu + "/api/close", {})
                lab.cargar_datos(filas)
            ops = lab.operaciones()
            st, r = 200, {"operacion": ops[0] if ops else None}
        else:
            st, r = lab.enviar()
        op = (r or {}).get("operacion") if isinstance(r, dict) else None
        estado = op["estado"] if op else f"http_{st}"
        lab.portal_api("POST", "/_lab/limpiar_fallos", {})
        fila = {"i": i, "expediente": filas[i]["expediente"], "fallo": fallo, "estado": estado,
                "motivo": op.get("motivo") if op else (r or {}).get("detail") if isinstance(r, dict) else None,
                "op": op["id"] if op else None, **extra,
                "post_vistos": lab.portal_api("GET", "/_lab/estadisticas")["peticiones_post"] - pp0}
        if estado == "incierta" and op:
            inciertas_vistas[op["id"]] = filas[i]["expediente"]
            time.sleep(6)                                 # que el motor reciba la marca y Monitor la lea
            fila["monitor"] = lab.revisiones_monitor()
            monitor_vio.append(bool((fila["monitor"].get("revisiones_pendientes") or 0) > 0
                                    or fila["monitor"].get("forms_requiere_persona")))
            campos = lab.analizar()
            lab.rellenar(campos, i)
            s2, _ = lab.enviar()
            fila["repetir_http"] = s2
            bloqueos += s2 == 409
        if estado == "rechazada" and fallo == "capacidad":
            fila["recibidos_vs_capacidad"] = lab.portal_api("GET", "/_lab/estadisticas")
            lab.portal_api("POST", "/_lab/limites", {"capacidad": 100000})
            campos = lab.analizar()
            lab.rellenar(campos, i)
            s3, r3 = lab.enviar()                         # NUEVA confirmación (simulada) tras el rechazo
            fila["tras_capacidad"] = (r3 or {}).get("operacion", {}).get("estado") if s3 == 200 else s3
        if fallo == "ne_apagado":
            fila["diag_durante"] = lab.diag().get("motor")
            lab.ne.iniciar()
        if fallo == "red_caida":
            fila["diag_durante"] = lab.diag().get("conectividad")
            lab.ne_api("POST", "/sim/connections/sim-a/state", {"state": "up"})
        res.append(fila)
    time.sleep(24)
    # La PERSONA comprueba el acuse de las inciertas en el portal propio (acción explícita; nunca reenvía)
    verificadas = []
    for o in lab.operaciones():
        if o["estado"] == "incierta" and o.get("verificable"):
            v = lab.verificar(o["id"])
            verificadas.append({"op": o["id"], "tras": v["operacion"]["estado"], "motivo": v.get("motivo")})
    ops = {o["id"]: o for o in lab.operaciones()}
    st1 = lab.portal_api("GET", "/_lab/estadisticas")
    recibidos = lab.recibidos_e10()[rec0:]
    # --- comprobaciones
    por_exp = {f["expediente"]: f for f in filas}
    mezclados = []
    for x in recibidos:
        c = x["campos"]
        f = por_exp.get(c.get("expediente"))
        if f is None or any(str(f.get(k)) != str(v) for k, v in c.items() if k in f):
            mezclados.append({"folio": x["folio"], "expediente": c.get("expediente")})
    conservadas = {oid: ops.get(oid, {}).get("estado") for oid in inciertas_vistas}
    perdidas = {k: v for k, v in conservadas.items() if v not in ("incierta", "confirmada")}
    d = lab.diag()
    return {"jornada": etiqueta, "expedientes": n, "fallos": fallos, "resultados": res, "verificadas": verificadas,
            "portal": {"recibidos": st1["recibidos"] - st0["recibidos"], "duplicados": st1["duplicados"],
                       "peticiones_post": st1["peticiones_post"] - st0["peticiones_post"],
                       "reintentos_mismo_token": st1["reintentos_mismo_token"] - st0["reintentos_mismo_token"],
                       "rechazos": st1["rechazos"]},
            "comprobaciones": {
                "sin_duplicados": st1["duplicados"] == 0,
                "sin_mezclas": not mezclados, "mezclados": mezclados,
                "inciertas_conservadas": not perdidas, "inciertas_perdidas": perdidas,
                "repeticiones_bloqueadas": all(f.get("repetir_http") == 409 for f in res if "repetir_http" in f),
                "doble_clic_un_envio": all(f.get("doble_clic_aceptados") == 1 for f in res if "doble_clic_aceptados" in f),
                "frase_incorrecta_sin_envio": all(f.get("frase_incorrecta_http", 400) >= 400
                                                  and f.get("post_tras_frase_incorrecta") == 0
                                                  for f in res if f.get("fallo") == "frase_incorrecta"),
                "capacidad_respetada": all(f["recibidos_vs_capacidad"]["recibidos"] <= f["recibidos_vs_capacidad"]["capacidad"]
                                           for f in res if "recibidos_vs_capacidad" in f),
                "monitor_ve_revision_de_cada_incierta": all(monitor_vio) if monitor_vio else None,
                "credencial_revocada_se_ve": all(f.get("diag_con_credencial_revocada") == "credencial_invalida"
                                                 for f in res if "diag_con_credencial_revocada" in f),
                "motor_conectado_al_final": d.get("motor") == "conectado"},
            "forms": {"estados": {s: sum(1 for o in ops.values() if o["estado"] == s) for s in {o["estado"] for o in ops.values()}}},
            "monitor_final": lab.revisiones_monitor(),
            "diagnostico_final": {k: d.get(k) for k in ("motor", "conectividad", "fase", "requiere_persona", "motivo",
                                                         "avisos_no_entregados", "operaciones_abiertas")}}


def auditoria(JL, lab) -> dict:
    """Escenarios que DEMUESTRAN (con datos ficticios, en el portal propio) los defectos A1 y D1 de Forms."""
    out = {}
    filas = JL.filas_ficticias(90000, 2)
    lab.cargar_datos(filas)
    # A1: el portal SÍ recibe (cortar_despues) y el acuse no responde a tiempo: incierta. El operador resuelve la marca
    # de RED en Network Engine. ¿Forms la da por «revisada» y deja reenviar? Con el token, el portal propio lo frena;
    # se repite con el portal SIN token para ver lo que pasaría con un portal real sin esa protección.
    for modo in (False, True):
        JL.http("POST", lab.pu + "/_lab/e10/modo", {"sin_token": modo})
        i = 0 if not modo else 1
        lab.rellenar(lab.analizar(), i)
        lab.portal_api("POST", "/_lab/fallo", {"tipo": "cortar_despues", "veces": 1})
        lab.portal_api("POST", "/_lab/fallo", {"tipo": "acuse_lento", "segundos": 25})
        st, r = lab.enviar()
        op = (r or {}).get("operacion") or {}
        lab.portal_api("POST", "/_lab/limpiar_fallos", {})
        time.sleep(8)                                     # la marca llega al motor
        sid = lab.diag().get("sesion_id")
        resuelto = None
        for _ in range(10):                               # el operador resuelve en el motor (admin); S2 puede pedir esperar
            s, body, _ = lab.ne_api("POST", f"/sessions/{sid}/resolve", {"note": "revisión de RED en laboratorio"})
            resuelto = s
            if s == 200:
                break
            time.sleep(3)
        time.sleep(10)
        tras = next((o for o in lab.operaciones() if o["id"] == op.get("id")), {})
        stp0 = lab.portal_api("GET", "/_lab/estadisticas")
        lab.rellenar(lab.analizar(), i)
        s2, r2 = lab.enviar()
        time.sleep(1)
        stp1 = lab.portal_api("GET", "/_lab/estadisticas")
        out["A1_" + ("portal_sin_token" if modo else "portal_con_token")] = {
            "estado_inicial": op.get("estado"), "motivo": op.get("motivo"), "resolve_http": resuelto,
            "estado_tras_resolver_en_ne": tras.get("estado"), "revision": tras.get("revision"),
            "reenvio_http": s2, "reenvio_estado": ((r2 or {}).get("operacion") or {}).get("estado"),
            "duplicados_en_portal_antes": stp0["duplicados"], "duplicados_en_portal_despues": stp1["duplicados"],
            "recibidos_en_portal_por_el_reenvio": stp1["recibidos"] - stp0["recibidos"],
            "tramites_del_mismo_expediente_en_portal": sum(1 for x in lab.recibidos_e10()
                                                           if x["campos"].get("expediente") == filas[i]["expediente"])}
    JL.http("POST", lab.pu + "/_lab/e10/modo", {"sin_token": False})
    # D1: Chromium reintenta por su cuenta un POST cortado; con el portal sin token es un duplicado
    filas = JL.filas_ficticias(91000, 1)
    lab.cargar_datos(filas)
    JL.http("POST", lab.pu + "/_lab/e10/modo", {"sin_token": True})
    stp0 = lab.portal_api("GET", "/_lab/estadisticas")
    lab.rellenar(lab.analizar(), 0)
    lab.portal_api("POST", "/_lab/fallo", {"tipo": "cortar_despues", "veces": 1})
    st, r = lab.enviar()
    lab.portal_api("POST", "/_lab/limpiar_fallos", {})
    stp1 = lab.portal_api("GET", "/_lab/estadisticas")
    out["D1_reintento_chromium_portal_sin_token"] = {
        "post_vistos": stp1["peticiones_post"] - stp0["peticiones_post"],
        "recibidos": stp1["recibidos"] - stp0["recibidos"],
        "duplicados_nuevos": stp1["duplicados"] - stp0["duplicados"],
        "estado_forms": ((r or {}).get("operacion") or {}).get("estado")}
    JL.http("POST", lab.pu + "/_lab/e10/modo", {"sin_token": False})
    return out


def capturas(JL, lab, carpeta: Path) -> dict:
    """Capturas (Chromium sin ventana, 2x) del centro de revisiones del Monitor y de Forms con una incierta. SIMULADO."""
    from playwright.sync_api import sync_playwright
    carpeta.mkdir(parents=True, exist_ok=True)
    filas = JL.filas_ficticias(95000, 1)
    lab.cargar_datos(filas)
    lab.rellenar(lab.analizar(), 0)
    lab.portal_api("POST", "/_lab/fallo", {"tipo": "cortar_despues", "veces": 1})
    lab.portal_api("POST", "/_lab/fallo", {"tipo": "acuse_lento", "segundos": 25})
    st, r = lab.enviar()
    lab.portal_api("POST", "/_lab/limpiar_fallos", {})
    time.sleep(10)
    hechas = []
    ruta = os.environ.get("CAUCE_BROWSER_PATH")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=ruta) if ruta else p.chromium.launch()
        pg = b.new_page(viewport={"width": 1366, "height": 900}, device_scale_factor=2)

        def foto(nombre: str) -> None:
            pg.wait_for_timeout(2500)
            pg.screenshot(path=str(carpeta / nombre), full_page=True)
            hechas.append(nombre)

        pg.goto(f"http://127.0.0.1:{lab.p_monitor}/")
        pg.fill("input[name=username]", "admin")
        pg.fill("input[name=password]", lab.mon_pwd)
        pg.press("input[name=password]", "Enter")
        pg.wait_for_timeout(2000)
        pg.goto(f"http://127.0.0.1:{lab.p_monitor}/#red")
        foto("01_monitor_red_con_incierta_de_forms.png")
        pg.goto(lab.fu + "/")
        foto("02_forms_con_operacion_incierta.png")
        sid = lab.diag().get("sesion_id")
        res = None
        for _ in range(10):
            res = lab.ne_api("POST", f"/sessions/{sid}/resolve", {"note": "revisión de RED (captura de laboratorio)"})[0]
            if res == 200:
                break
            time.sleep(3)
        time.sleep(10)
        pg.goto(f"http://127.0.0.1:{lab.p_monitor}/#red")
        pg.reload()
        foto("03_monitor_red_tras_resolver_en_ne.png")
        pg.goto(lab.fu + "/")
        foto("04_forms_tras_resolver_en_ne.png")
        b.close()
    return {"carpeta": str(carpeta), "capturas": hechas, "envio_http": st,
            "estado_inicial": ((r or {}).get("operacion") or {}).get("estado"), "resolve_http": res,
            "estado_tras_resolver": [o["estado"] for o in lab.operaciones()][:3],
            "nota": "SIMULADO: portal propio, datos ficticios, red simulada."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forms", required=True)
    ap.add_argument("--monitor", required=True)
    ap.add_argument("--monitor-python", required=True)
    ap.add_argument("--expedientes", type=int, nargs="*", default=[6, 10, 20, 50])
    ap.add_argument("--duracion-min", type=float, default=0)
    ap.add_argument("--auditoria", action="store_true")
    ap.add_argument("--capturas", default="")
    ap.add_argument("--salida", required=True)
    a = ap.parse_args(argv)
    forms, salida = Path(a.forms).resolve(), Path(a.salida).resolve()
    JL = cargar_jl(forms)
    lab = construir(JL, forms, Path(a.monitor).resolve(), a.monitor_python, salida)
    commits = {"network_engine": subprocess.run(["git", "-C", str(NE_REPO), "rev-parse", "HEAD"], capture_output=True,
                                                text=True).stdout.strip(),
               "forms": subprocess.run(["git", "-C", str(forms), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
               "monitor": subprocess.run(["git", "-C", a.monitor, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()}
    t0 = time.time()
    med = JL.Medidor(lab.procs, paso=15.0)
    jornadas, aud = [], None
    lab.iniciar()
    med.start()
    try:
        if a.capturas:
            aud = capturas(JL, lab, Path(a.capturas).resolve())
        elif a.auditoria:
            aud = auditoria(JL, lab)
        elif a.duracion_min:
            k = 0
            while time.time() - t0 < a.duracion_min * 60:
                jornadas.append(jornada(JL, lab, 10, 1 + k * 10, f"prolongada-{k + 1}"))
                k += 1
                (salida / "jornadas.json").write_text(json.dumps({"commits": commits, "jornadas": jornadas,
                                                                 "en_curso": True}, ensure_ascii=False, indent=1), "utf-8")
        else:
            desde = 1
            for n in a.expedientes:
                jornadas.append(jornada(JL, lab, n, desde, f"jornada-{n}"))
                desde += n
    finally:
        med.parar.set()
        med.join(20)
        tam = JL.tamanos(lab) if hasattr(JL, "tamanos") else {}
        lab.detener()
        residuales = [x.pid for p in lab.procs.values() for x in p.arbol()]
    out = {"commits": commits, "duracion_min": round((time.time() - t0) / 60, 1), "jornadas": jornadas,
           "auditoria": aud, "tamanos": tam, "procesos_residuales": residuales,
           "muestras": JL.resumen_muestras(med.muestras) if med.muestras else None,
           "tiempos": JL.resumen_tiempos(lab.tiempos), "eventos": lab.eventos[-200:],
           "nota": "SIMULADO: red simulada del motor, portal propio en 127.0.0.1, datos ficticios y confirmación humana "
                   "simulada por el guion. No es una prueba física."}
    (salida / ("capturas.json" if a.capturas else "auditoria.json" if a.auditoria else "jornadas.json")).write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    (salida / "muestras.json").write_text(json.dumps(med.muestras), "utf-8")
    for j in jornadas:
        c = j["comprobaciones"]
        print(f"{j['jornada']}: " + " · ".join(f"{k}={v}" for k, v in c.items() if not isinstance(v, (list, dict))), flush=True)
    if aud:
        print(json.dumps(aud, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
