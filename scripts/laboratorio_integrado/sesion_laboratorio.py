"""Sesión de navegador de laboratorio (paso 09b de la validación integral).

Recorre las 8 fases de una sesión de Network Engine con un Chromium REAL abierto en el portal propio de laboratorio
(el formulario que sirve la interfaz de Forms en 127.0.0.1) y la red SIMULADA del laboratorio:
iniciada → activa → pérdida de conectividad → recuperada → requiere intervención (operación a medias interrumpida) →
incierta (sin latidos) → interrumpida (navegador cerrado) → cerrada.

Hace de «dueño» de la sesión lo que hará Cauce Forms cuando se integre (sin modificar Forms): latidos ok/error y si hay
una operación en curso. Nunca envía el formulario. Comprueba que tras la interrupción nada se repite solo: el formulario
sigue sin enviarse y Network Engine no lanzó ninguna tarea.

Se ejecuta en dos partes: el proceso padre (Python del laboratorio) crea una credencial `session` temporal y lanza el
hijo con el Python de Forms (que tiene Playwright); los tokens viajan por variables de entorno, nunca por argumentos.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(port: int, ruta: str, token: str, metodo: str = "GET", cuerpo=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1{ruta}", method=metodo,
                                 data=json.dumps(cuerpo).encode() if cuerpo is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with _OPENER.open(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


# ------------------------------------------------------------------------------------------------ padre
def prueba(it) -> dict:
    """Llamada desde integral.Integral.p09b_sesiones."""
    import lab as L
    lb = it.lab
    if not (lb.estado.get("navegador_forms") or os.environ.get("CAUCE_LAB_BROWSER_PATH")):
        return {"estado": "PENDIENTE", "evidencia": "LOCAL", "detalle": "sin Chromium para Forms (1_INSTALAR.cmd --navegador-forms)"}
    port, adm = lb.p["network_engine"], lb.admin_ne()
    st, tok = _http(port, "/tokens", adm, "POST", {"name": "lab-sesion", "scope": "session"})
    if st not in (200, 201) or not isinstance(tok, dict):
        return {"estado": "FALLA", "detalle": f"no se pudo crear la credencial de sesión (HTTP {st})"}
    _, tareas0 = _http(port, "/tasks", adm)
    env = lb.entorno_forms()
    env.update({"CAUCE_LAB_NE_PORT": str(port), "CAUCE_LAB_NE_ADMIN": adm, "CAUCE_LAB_NE_SESSION": tok["token"],
                "CAUCE_LAB_FORMS_URL": f"http://127.0.0.1:{lb.p['forms']}/lab/01_tradicional.html"})
    try:
        r = subprocess.run([lb.py("forms"), os.path.abspath(__file__), "--hijo"], cwd=str(lb.forms), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=420)
    finally:
        _http(port, f"/tokens/{tok['id']}", adm, "DELETE")
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"estado": "FALLA", "detalle": f"el recorrido no devolvió resultados: {(r.stderr or '')[-400:]}"}
    _, tareas1 = _http(port, "/tasks", adm)
    n0, n1 = len((tareas0 or {}).get("tasks") or []), len((tareas1 or {}).get("tasks") or [])
    res["tareas_lanzadas_por_el_motor"] = n1 - n0
    vistas = [f for f, ok in res.get("fases", {}).items() if ok]
    faltan = [f for f, ok in res.get("fases", {}).items() if not ok]
    sin_repeticion = res.get("formulario_enviado") is False and n1 == n0
    # tras una operación interrumpida, recuperar la red NO basta: sigue pidiendo a una persona hasta que la resuelva
    ok = (not faltan and sin_repeticion and res.get("sigue_pidiendo_persona_tras_recuperar") is True
          and not res.get("error"))
    if ok:
        it.mf("F4", "Aprobado en simulación", "sesión con Chromium real en el portal propio; red simulada", "sesión de laboratorio")
        it.mf("F9", "Aprobado en simulación", "la sesión pasa por pérdida, recuperación e intervención humana", "sesión de laboratorio")
    (it.dir / "sesion_laboratorio.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), "utf-8")
    return {"estado": "PASA" if ok else "FALLA", "evidencia": "SIMULADA",
            "detalle": (f"fases vistas: {', '.join(vistas)}" + (f"; FALTAN: {', '.join(faltan)}" if faltan else "")
                        + f"; formulario enviado: {res.get('formulario_enviado')}; tareas del motor: {n1 - n0}"
                        + f"; sigue pidiendo persona tras recuperar: {res.get('sigue_pidiendo_persona_tras_recuperar')}"
                        + (f"; error: {res['error']}" if res.get("error") else "")),
            "tiempos_s": res.get("tiempos")}


# ------------------------------------------------------------------------------------------------ hijo
def _hijo() -> int:
    port = int(os.environ["CAUCE_LAB_NE_PORT"])
    adm, ses, url = os.environ["CAUCE_LAB_NE_ADMIN"], os.environ["CAUCE_LAB_NE_SESSION"], os.environ["CAUCE_LAB_FORMS_URL"]
    fases = {f: False for f in ("iniciada", "activa", "perdida_conectividad", "recuperada", "requiere_intervencion",
                                "incierta", "interrumpida", "cerrada")}
    tiempos: dict[str, float] = {}
    out: dict = {"fases": fases, "tiempos": tiempos, "formulario_enviado": None}
    sid = None
    pagina = None
    op = ["idle"]

    def estado():
        _, s = _http(port, f"/sessions/{sid}", adm)
        return s or {}

    def latir(ok: bool = True, codigo=None):
        cuerpo = {"status": "ok" if ok else "error", "operation": op[0]}
        if not ok:
            cuerpo["error_code"] = codigo
        return _http(port, f"/sessions/{sid}/heartbeat", ses, "POST", cuerpo)

    def pagina_viva() -> bool:
        try:
            return pagina is not None and not pagina.is_closed() and pagina.evaluate("document.readyState") == "complete"
        except Exception:  # noqa: BLE001
            return False

    def esperar_fase(fase: str, limite: float, latiendo: bool = True) -> bool:
        t0 = time.time()
        while time.time() - t0 < limite:
            if latiendo:
                latir(pagina_viva(), None if pagina_viva() else "browser_closed")
            if estado().get("phase") == fase:
                fases[fase] = True
                tiempos[fase] = round(time.time() - t0, 1)
                return True
            time.sleep(0.8)
        return False

    def red(estado_red: str):
        _http(port, "/sim/connections/sim-a/state", adm, "POST", {"state": estado_red})

    try:
        from playwright.sync_api import sync_playwright
        st, s = _http(port, "/sessions", ses, "POST", {"label": "lab-forms", "connection_id": "sim-a",
                                                         "authorized_by_user": True, "heartbeat_timeout_s": 6})
        if st != 201:
            raise RuntimeError(f"registro rechazado: HTTP {st} {s}")
        sid = s["id"]
        fases["iniciada"] = s.get("phase") == "iniciada"
        kw = {"headless": True}
        if os.environ.get("CAUCE_BROWSER_PATH"):
            kw["executable_path"] = os.environ["CAUCE_BROWSER_PATH"]
        with sync_playwright() as p:
            nav = p.chromium.launch(**kw)
            pagina = nav.new_page()
            envios = []
            pagina.on("request", lambda r: envios.append(r.method) if r.method == "POST" else None)
            pagina.goto(url)
            esperar_fase("activa", 15)
            red("down")
            esperar_fase("perdida_conectividad", 40)
            red("up")
            esperar_fase("recuperada", 60)
            # operación a medias (se escribe en un campo, NO se envía) y corte
            op[0] = "in_progress"
            latir()
            campo = pagina.query_selector("input[type=text], input:not([type])")
            if campo:
                campo.fill("Prueba ficticia")
            red("down")
            esperar_fase("requiere_intervencion", 40)
            red("up")
            op[0] = "idle"
            for _ in range(6):
                latir()
                time.sleep(1.0)
            out["sigue_pidiendo_persona_tras_recuperar"] = estado().get("requires_human") is True
            _http(port, f"/sessions/{sid}/resolve", adm, "POST", {})
            # sin latidos: incierta
            esperar_fase("incierta", 20, latiendo=False)
            esperar_fase("recuperada", 15)
            # el navegador se cierra: interrumpida
            pagina.close()
            esperar_fase("interrumpida", 15)
            out["formulario_enviado"] = bool(envios)
            nav.close()
        _http(port, f"/sessions/{sid}", ses, "DELETE")
        _, resumen = _http(port, "/status/sessions", adm)
        fases["cerrada"] = any(c.get("label") == "lab-forms" and c.get("phase") == "cerrada"
                               for c in (resumen or {}).get("closed") or [])
        sid = None
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:300]
        red("up")
        if sid:
            _http(port, f"/sessions/{sid}", ses, "DELETE")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__" and "--hijo" in sys.argv:
    raise SystemExit(_hijo())
