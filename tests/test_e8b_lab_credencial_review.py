"""E8b: el laboratorio integrado prepara SOLO para sí mismo la credencial «review» del centro de revisiones de Monitor.

Con un Network Engine REAL (simulación, 127.0.0.1) arrancado por el propio lab.py: la credencial se crea con la orden
oficial, en un archivo distinto del de lectura, dentro de la carpeta marcada del laboratorio; solo llega a las
revisiones pendientes (lectura y estado: 403) y la credencial de lectura no llega a las revisiones."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts" / "laboratorio_integrado"))
import lab  # noqa: E402
from cauce.client import CauceAPIError, CauceClient  # noqa: E402
from cauce.secrets_store import read_secret  # noqa: E402


class LabCredencialReviewTest(unittest.TestCase):
    def test_nombre_fuera_de_los_prefijos_que_se_limpian(self):
        self.assertFalse(lab.NOMBRE_CREDENCIAL_REVIEW.startswith(lab.PREFIJOS_DEL_LABORATORIO))

    def test_rechaza_admin_y_archivos_fuera_del_laboratorio(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            with mock.patch.object(lab.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    l.crear_credencial(l.datos("x.token"), "monitor", "admin")
                with self.assertRaises(ValueError):
                    l.crear_credencial(Path(d) / "fuera.token", "monitor", "review")
                run.assert_not_called()

    def test_monitor_recibe_el_archivo_solo_si_existe(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            l.estado["monitor_admin"] = "x"
            envs = []
            with mock.patch.object(l, "_lanzar", side_effect=lambda n, a, c, e: envs.append(e) or 1), \
                    mock.patch.object(l, "_esperar_puerto", return_value=True):
                l.iniciar_monitor()
                l.datos("monitor-revisiones.token").parent.mkdir(parents=True, exist_ok=True)
                l.datos("monitor-revisiones.token").write_text("x")
                l.iniciar_monitor()
            self.assertNotIn("MONITOREO_NETWORK_REVISIONES_TOKEN_FILE", envs[0])
            self.assertEqual(envs[1]["MONITOREO_NETWORK_REVISIONES_TOKEN_FILE"], str(l.datos("monitor-revisiones.token")))
            self.assertNotEqual(envs[1]["MONITOREO_NETWORK_REVISIONES_TOKEN_FILE"], envs[1]["MONITOREO_NETWORK_TOKEN_FILE"])

    def test_con_network_engine_real(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"CAUCE_LAB_DESPLAZAMIENTO_PUERTOS": "437"}), \
                mock.patch.object(lab.Lab, "ne", new=property(lambda s: RAIZ)):   # este mismo Network Engine
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            l.estado["forms"] = False
            try:
                with mock.patch.object(l, "iniciar_monitor", return_value=True):
                    r = l.iniciar("simulado", None, abrir=False)
                self.assertTrue(r["ok"], r)
                self.assertTrue(r["servicios"]["credencial_review"] and r["servicios"]["credencial_read"], r)
                puerto = l.p["network_engine"]
                rev = CauceClient(read_secret(str(l.datos("monitor-revisiones.token"))), port=puerto)
                lec = CauceClient(read_secret(str(l.datos("monitor.token"))), port=puerto)
                self.assertIsInstance(rev.pending_reviews(), list)          # review: ve las revisiones
                for c, ruta in ((rev, "/status/connectivity"), (rev, "/status/sessions"), (lec, "/sessions/pending")):
                    with self.assertRaises(CauceAPIError) as e:
                        c._call("GET", ruta)
                    self.assertEqual(e.exception.status, 403, ruta)
                admin = CauceClient(l.admin_ne(), port=puerto)
                por_nombre = {t["name"]: t for t in admin._call("GET", "/tokens")["tokens"]}
                self.assertEqual(por_nombre[lab.NOMBRE_CREDENCIAL_REVIEW]["scope"], "review")
                self.assertEqual(por_nombre["monitor-lab"]["scope"], "read")
                # segunda vez: no crea otra
                with mock.patch.object(l, "iniciar_monitor", return_value=True), \
                        mock.patch.object(l, "crear_credencial") as crear, mock.patch.object(l, "iniciar_ne", return_value=True):
                    l.iniciar("simulado", None, abrir=False)
                crear.assert_not_called()
            finally:
                l.detener(forzar=True)


if __name__ == "__main__":
    unittest.main()
