"""E4: las 8 fases de una sesión de navegador y la regla de no repetir operaciones interrumpidas."""
import unittest

from test_sessions import SessionApiBase
from cauce.client import CauceAPIError


class FasesTest(SessionApiBase):
    def fase(self):
        return self.read.sessions()["sessions"][0]

    def test_recorrido_completo_de_fases(self):
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.assertEqual(self.fase()["phase"], "iniciada")
        self.beat(self.a)
        self.assertEqual(self.fase()["phase"], "activa")
        self.fail_link("sim-a")
        self.assertEqual(self.fase()["phase"], "perdida_conectividad")
        self.recover_link("sim-a")
        self.beat(self.a)
        f = self.fase()
        self.assertEqual((f["phase"], f["recovered"]["from"]), ("recuperada", "network_down"))
        self.clock.advance(200)                       # pasa la ventana de «recuperada» ... y el latido caduca
        self.assertEqual(self.fase()["phase"], "incierta")
        self.beat(self.a)
        self.assertEqual((self.fase()["phase"], self.fase()["recovered"]["from"]), ("recuperada", "stale"))
        self.beat(self.a, ok=False, code="page_unreachable")
        self.assertEqual(self.fase()["phase"], "interrumpida")
        self.a.close()
        s = self.read.sessions()
        self.assertEqual(s["sessions"], [])
        self.assertEqual((s["closed"][0]["phase"], s["closed"][0]["label"]), ("cerrada", "forms-a"))

    def test_operacion_interrumpida_exige_persona_y_no_se_limpia_sola(self):
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        self.a.heartbeat(True, operation="in_progress")
        self.fail_link("sim-a")
        f = self.fase()
        self.assertEqual((f["phase"], f["requires_human"], f["human_reason"]),
                         ("requiere_intervencion", True, "operation_interrupted"))
        self.recover_link("sim-a")
        self.clock.advance(1)
        self.a.heartbeat(True, operation="idle")
        self.assertTrue(self.fase()["requires_human"], "ni la recuperación ni el dueño la dan por revisada")
        with self.assertRaises(CauceAPIError) as e:
            self.a._c._call("POST", f"/sessions/{self.a.session_id}/resolve")
        self.assertEqual(e.exception.status, 403)
        self.admin._call("POST", f"/sessions/{self.a.session_id}/resolve")
        self.assertEqual(self.fase()["phase"], "recuperada", "revisada por el operador: vuelve a su fase normal")
        kinds = [ev["kind"] for ev in self.eng.audit.events(limit=1000)]
        self.assertIn("session.requires_human", kinds)
        self.assertIn("session.human_resolved", kinds)

    def test_operacion_empezada_con_la_red_ya_caida_pide_persona(self):
        """E5 (capturas): antes solo se marcaba si el corte llegaba DURANTE la operación."""
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        self.a.heartbeat(True, operation="idle")
        self.fail_link("sim-a")
        self.assertFalse(self.fase()["requires_human"])
        self.clock.advance(1)
        self.a.heartbeat(True, operation="in_progress")
        f = self.fase()
        self.assertEqual((f["requires_human"], f["human_reason"]), (True, "operation_interrupted"))

    def test_sin_operacion_en_curso_un_corte_no_pide_persona(self):
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        self.a.heartbeat(True, operation="idle")
        self.fail_link("sim-a")
        self.assertFalse(self.fase()["requires_human"])

    def test_sesion_caducada_pide_persona(self):
        self.a.register("forms-a", heartbeat_timeout_s=10)
        self.beat(self.a, ok=False, code="session_expired")
        self.assertEqual((self.fase()["phase"], self.fase()["human_reason"]), ("requiere_intervencion", "session_expired"))

    def test_operacion_invalida_se_rechaza(self):
        self.a.register("forms-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        with self.assertRaises(CauceAPIError) as e:
            self.a.heartbeat(True, operation="enviar")
        self.assertEqual(e.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
