"""Forms FICTICIO para probar verificar_sesiones_forms.py (no es Forms; no abre navegador ni envía nada).

Modo «correcto»: usa el consumidor de referencia de RC3 tal cual, una etiqueta estable y una sola operación que nunca
repite. Modo «repite»: tras la operación vuelve a ponerla «en curso» (lo que un Forms NO debe hacer) para demostrar que
el verificador lo detecta. Lee el puerto y el archivo de la credencial de las variables de entorno."""
import os
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(RAIZ))
from cauce.client import CauceSessionClient  # noqa: E402
from cauce.secrets_store import read_secret  # noqa: E402
from cauce.session_consumer import SesionDiagnostico  # noqa: E402

modo, etiqueta = sys.argv[1], sys.argv[2]
cli = CauceSessionClient(read_secret(os.environ["CAUCE_NE_TOKEN_FILE"]), port=int(os.environ["CAUCE_NE_PUERTO"]), timeout=5)
s = SesionDiagnostico(cli, etiqueta)
s.iniciar()
for _ in range(4):
    time.sleep(1)
    s.latir()
with s.operacion():
    time.sleep(4)                       # «clic y espera» del envío (aquí no se envía nada)
for _ in range(6):
    time.sleep(1)
    s.latir()
if modo == "repite":
    time.sleep(0.6)
    s.latir(operation="in_progress")    # PROHIBIDO por contrato: repetir sola una operación incierta
    time.sleep(2)
    s.latir(operation="idle")
for _ in range(3):
    time.sleep(1)
    s.latir()
s.cerrar()
