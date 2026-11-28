"""E6 (demostrado en el laboratorio): tras una validación cortada, su credencial y su sesión de prueba seguían en el
motor (las sesiones se conservan tras reiniciar) y la siguiente ejecución recibía 409 en los pasos 09b y 14.
Motor real por HTTP, red simulada."""
import sys
import unittest
from pathlib import Path

import helpers  # noqa: F401
from helpers import sim_engine
from cauce.api import CauceAPI
from cauce.client import CauceAPIError, CauceClient, CauceSessionClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "laboratorio_integrado"))
import lab as L  # noqa: E402


class SobrantesTest(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine(sessions={"max": 8})
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.admin = CauceClient(self.api.token, port=self.api.port)

    def tearDown(self):
        self.api.stop()

    def ne(self, ruta, metodo="GET", cuerpo=None):
        try:
            return 200, self.admin._call(metodo, ruta, cuerpo)
        except CauceAPIError as e:
            return e.status, None

    def sesion(self, nombre, etiqueta, en_curso=False):
        t = self.admin.create_token(nombre, "session")
        c = CauceSessionClient(t["token"], port=self.api.port)
        c.register(etiqueta, connection_id="sim-a")
        if en_curso:
            c.heartbeat(operation="in_progress")
        return c

    def test_lo_sobrante_se_limpia_sin_dar_nada_por_revisado(self):
        self.sesion("lab-sesion", "lab-forms")                         # cortada sin operación a medias
        marcada = self.sesion("lab-supervision", "lab-supervision", en_curso=True)
        # la marcamos como lo haría un reinicio del motor durante la operación
        s = self.eng.sessions._sessions[marcada.session_id]
        s.requires_human, s.human_reason = True, "engine_restart_during_operation"
        ajena = self.sesion("forms-real", "forms-a")                    # no es del laboratorio: no se toca

        r = L.limpiar_sobrantes_del_laboratorio(self.ne)
        self.assertEqual(r["sesiones_cerradas"], 1)
        self.assertEqual(r["sesiones_que_piden_revision"], ["lab-supervision"])
        self.assertEqual(r["credenciales_revocadas"], 2)
        vivas = {x["label"]: x for x in self.admin._call("GET", "/status/sessions")["sessions"]}
        self.assertIn("forms-a", vivas, "lo ajeno sigue intacto")
        # E10: revocar su credencial cierra la sesión marcada, pero la marca nunca se quita sola: queda pendiente
        pend = self.admin._call("GET", "/sessions/pending")["pending"]
        self.assertTrue(any(p["label"] == "lab-supervision" for p in pend), "una marca nunca se quita sola")
        self.assertNotIn("lab-forms", vivas)
        self.assertTrue(ajena.heartbeat()["id"])

        nueva = self.sesion(L.etiqueta_unica("lab-sesion"), L.etiqueta_unica("lab-supervision"))
        self.assertFalse(nueva.get()["requires_human"], "la ejecución nueva no hereda la marca de la cortada")


if __name__ == "__main__":
    unittest.main()
