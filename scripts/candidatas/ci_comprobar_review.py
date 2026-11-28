"""E8b (CI): con el laboratorio RC3 en marcha, Monitor tiene la credencial «review» del laboratorio (archivo distinto del de
lectura, dentro de la carpeta RC3) y puede resolver revisiones; el laboratorio de RC2 no tiene ninguna credencial review.
Uso: python ci_comprobar_review.py CARPETA_LAB_RC3"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

lab = Path(sys.argv[1])
rc2 = Path(os.environ.get("USERPROFILE") or Path.home()) / "cauce-lab-integrado"
fallos = []
lec, rev = lab / "datos" / "monitor.token", lab / "datos" / "monitor-revisiones.token"
if not (lec.is_file() and rev.is_file()):
    fallos.append(f"faltan credenciales: lectura {lec.is_file()}, review {rev.is_file()}")
elif lec.read_bytes() == rev.read_bytes():
    fallos.append("la credencial review es la misma que la de lectura")
if list(rc2.rglob("monitor-revisiones.token")):
    fallos.append("el laboratorio de RC2 tiene una credencial review (no debía tocarse)")
est = json.loads((lab / "estado.json").read_text("utf-8"))
puerto = 8100 + int(os.environ.get("CAUCE_LAB_DESPLAZAMIENTO_PUERTOS", "100"))
base = f"http://127.0.0.1:{puerto}"


def pedir(ruta, cuerpo=None, token=None):
    req = urllib.request.Request(base + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


tok = pedir("/api/auth/login", {"username": "admin", "password": est["monitor_admin"]})["token"]
red = {}
for _ in range(60):
    red = pedir("/api/red", token=tok)
    if red.get("revisiones_resolubles") is not None and red.get("disponible", True):
        break
    time.sleep(2)
if red.get("revisiones_resolubles") is not True:
    fallos.append(f"Monitor no puede resolver revisiones: {red.get('revisiones_resolubles')!r}")

# De punta a punta: una marca «revisar a mano» sin sesión en el motor del laboratorio RC3 y Monitor la resuelve con
# la credencial review (admin de Monitor, nota obligatoria y etiqueta como confirmación).
RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
from cauce.client import CauceClient, CauceSessionClient  # noqa: E402
from cauce.secrets_store import read_secret  # noqa: E402

pne = 8880 + int(os.environ.get("CAUCE_LAB_DESPLAZAMIENTO_PUERTOS", "100"))
adm = CauceClient(read_secret(str(lab / "datos" / "network-engine" / "api.token")), port=pne)
etiqueta = f"lab-sesion-ci-{int(time.time())}"
t = adm.create_token(etiqueta, "session")
sc = CauceSessionClient(t["token"], port=pne)
sc.register(etiqueta)
time.sleep(0.6)
sc.heartbeat(ok=False, error_code="other", operation="in_progress")
sc.close()
marca = next((m for m in adm.pending_reviews() if m.get("label") == etiqueta), None)
resuelta = None
if marca is None:
    fallos.append("no se creó la marca de prueba")
else:
    for _ in range(30):
        red = pedir("/api/red", token=tok)
        if any(r.get("id") == marca["id"] for r in ((red.get("sesiones") or {}).get("revisiones") or [])):
            break
        time.sleep(2)
    try:
        sin_nota = pedir(f"/api/red/revisiones/{marca['id']}/resolver", {"nota": "", "confirmar": etiqueta}, tok)
        fallos.append(f"se aceptó resolver sin nota: {sin_nota}")
    except urllib.error.HTTPError as e:
        if e.code != 400:
            fallos.append(f"sin nota debía dar 400 y dio {e.code}")
    try:
        resuelta = pedir(f"/api/red/revisiones/{marca['id']}/resolver",
                         {"nota": "CI: comprobado en el portal de laboratorio (ficticio)", "confirmar": etiqueta}, tok)
    except urllib.error.HTTPError as e:
        fallos.append(f"Monitor no resolvió la marca: HTTP {e.code} {e.read()[:200]!r}")
    if any(m.get("id") == marca["id"] for m in adm.pending_reviews()):
        fallos.append("la marca sigue pendiente en el motor")
adm._call("DELETE", f"/tokens/{t['id']}")
print(json.dumps({"revisiones_resolubles": red.get("revisiones_resolubles"), "resuelta": resuelta,
                  "resumen_interfaces": red.get("resumen_interfaces"), "fallos": fallos}, ensure_ascii=False))
sys.exit(1 if fallos else 0)
