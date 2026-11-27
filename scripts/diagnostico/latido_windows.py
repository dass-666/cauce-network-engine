"""E8 · Diagnóstico del intermitente de Windows (test_contrato_forms: 'sin_informacion' != 'activa').

Hipótesis a medir, no demostrada: el latido guarda en disco de forma síncrona; en Windows un antivirus o el indexador
puede retener el archivo, `_replace` reintenta (hasta ~4 s) y la respuesta supera los 2 s de espera del cliente.

Repite N ciclos registro → en curso → terminado contra la API real (red simulada, loopback) y anota la latencia de cada
latido, la duración de cada guardado y los reintentos por PermissionError. Uso:
    python scripts/diagnostico/latido_windows.py --ciclos 200 --salida reports/diagnostico-latido.json"""
import argparse
import json
import os
import statistics
import sys
import time

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "tests"))
import cauce.session_store as almacen  # noqa: E402
from helpers import sim_engine  # noqa: E402
from cauce.api import CauceAPI  # noqa: E402
from cauce.client import CauceClient, CauceSessionClient  # noqa: E402
from cauce.session_consumer import SesionDiagnostico  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--ciclos", type=int, default=200)
ap.add_argument("--salida", default="reports/diagnostico-latido.json")
a = ap.parse_args()

guardados, reintentos = [], [0]
real_save, real_replace = almacen.SessionStore._save, os.replace


def save(self, *x, **k):
    t = time.perf_counter()
    try:
        return real_save(self, *x, **k)
    finally:
        guardados.append(time.perf_counter() - t)


def replace(src, dst):
    try:
        return real_replace(src, dst)
    except PermissionError:
        reintentos[0] += 1
        raise


almacen.SessionStore._save = save
almacen.os.replace = replace          # solo el módulo del almacén ve el contador

eng, _ = sim_engine(sessions={"max": 4})
api = CauceAPI(eng, "127.0.0.1", 0)
api.start()
adm = CauceClient(api.token, port=api.port)
latidos, fallos = [], []
try:
    for i in range(a.ciclos):
        tok = adm.create_token(f"diag-{i}", "session")
        cli = CauceSessionClient(tok["token"], port=api.port)
        orig = cli._c._call

        def medido(method, path, body=None, _o=orig):
            t = time.perf_counter()
            try:
                return _o(method, path, body)
            finally:
                if path.endswith("/heartbeat"):
                    latidos.append(time.perf_counter() - t)
        cli._c._call = medido
        s = SesionDiagnostico(cli, f"diag-{i}", conexion="sim-a", latido_max_s=30)
        s.iniciar()
        with s.operacion():
            pass
        if s.ultimo.estado != "activa":
            fallos.append({"ciclo": i, "estado": s.ultimo.estado, "motivo": s.ultimo.motivo, "detalle": s.ultimo.detalle})
        s.cerrar()
        adm.revoke_token(tok["id"])
finally:
    api.stop()


def resumen(xs):
    if not xs:
        return {}
    xs = sorted(xs)
    return {"n": len(xs), "mediana_ms": round(statistics.median(xs) * 1000, 1),
            "p99_ms": round(xs[min(len(xs) - 1, int(len(xs) * 0.99))] * 1000, 1), "max_ms": round(xs[-1] * 1000, 1),
            "mayores_de_1s": sum(x > 1 for x in xs), "mayores_de_2s": sum(x > 2 for x in xs)}


out = {"plataforma": sys.platform, "python": sys.version.split()[0], "ciclos": a.ciclos, "fallos": fallos,
       "latidos": resumen(latidos), "guardados": resumen(guardados), "reintentos_permissionerror": reintentos[0]}
os.makedirs(os.path.dirname(os.path.abspath(a.salida)), exist_ok=True)
with open(a.salida, "w", encoding="utf-8") as fh:
    json.dump(out, fh, indent=2, ensure_ascii=False)
print(json.dumps(out, indent=2, ensure_ascii=False))
sys.exit(1 if fallos else 0)
