"""E5: pruebas de CONTRATO para la integración nativa de sesiones en Cauce Forms (solo diagnóstico).

Se usa el consumidor de referencia (cauce/session_consumer.py) contra el motor real (API HTTP en 127.0.0.1, red
simulada). Forms no se modifica: estas pruebas fijan lo que cualquier implementación de Forms debe cumplir.
"""
import time
import unittest

from helpers import sim_engine
from cauce.api import CauceAPI
from cauce.client import CauceClient, CauceSessionClient
from cauce.session_consumer import (CREDENCIAL_INVALIDA, SIN_INFORMACION, SesionDiagnostico,
                                    campos_enviados_permitidos)


class Espia(CauceSessionClient):
    """Registra cada cuerpo enviado para comprobar la lista blanca."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.enviados = []
        real = self._c._call

        def call(method, path, body=None):
            self.enviados.append((method, path, dict(body) if body else None))
            return real(method, path, body)
        self._c._call = call


class ContratoBase(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine()
        self.d = self.eng.cfg["data_dir"]
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.port = self.api.port
        self.admin = CauceClient(self.api.token, port=self.port)
        self.tok = self.admin.create_token("forms-lab", "session")
        self.cli = Espia(self.tok["token"], port=self.port)
        self.s = SesionDiagnostico(self.cli, "forms-lab", conexion="sim-a", latido_max_s=30)

    def tearDown(self):
        self.api.stop()

    def reiniciar_motor(self, persist=True):
        """Para el motor y arranca otro sobre la MISMA carpeta de datos y el MISMO puerto."""
        self.api.stop()
        self.eng, self.net = sim_engine(data_dir=self.d, sessions={"persist": persist})
        self.api = CauceAPI(self.eng, "127.0.0.1", self.port)
        self.api.start()
        self.admin = CauceClient(self.api.token, port=self.port)

    def apagar_motor(self):
        self.api.stop()


class ContratoTest(ContratoBase):
    def test_ciclo_normal_y_solo_campos_permitidos(self):
        self.assertEqual(self.s.iniciar().estado, "iniciada")
        acciones = []
        with self.s.operacion():
            acciones.append("enviar")                 # la acción de la persona
        self.assertEqual(acciones, ["enviar"])
        # E8: si falla, el mensaje dice por qué (intermitente visto una vez en Windows · Py3.10)
        self.assertEqual(self.s.ultimo.estado, "activa", (self.s.ultimo, [(m, p) for m, p, _ in self.cli.enviados]))
        self.s.cerrar()
        permitidos = campos_enviados_permitidos()
        for method, path, body in self.cli.enviados:
            if body is None:
                continue
            tipo = "heartbeat" if path.endswith("/heartbeat") else "register"
            self.assertLessEqual(set(body), permitidos[tipo], (path, body))
        ops = [b.get("operation") for _, p, b in self.cli.enviados if b and p.endswith("/heartbeat")]
        self.assertEqual(ops, ["in_progress", "idle"])

    def test_motor_apagado_no_bloquea_la_accion_y_dice_sin_informacion(self):
        self.s.iniciar()
        self.apagar_motor()
        acciones = []
        with self.s.operacion():
            acciones.append("enviar")
        self.assertEqual(acciones, ["enviar"], "la acción de la persona no depende del motor")
        self.assertEqual(self.s.ultimo.estado, SIN_INFORMACION)
        self.assertEqual(self.s.marcas_no_enviadas, 2)
        self.assertFalse(self.s.ultimo.puede_reintentar_automaticamente)

    def test_reinicio_del_motor_a_mitad_de_operacion_exige_persona(self):
        self.s.iniciar()
        acciones = []
        with self.s.operacion():
            acciones.append("enviar")
            self.reiniciar_motor()                    # el motor cae mientras la persona envía
        self.assertEqual(acciones, ["enviar"], "nada se repite")
        e = self.s.ultimo
        self.assertEqual((e.requiere_persona, e.motivo), (True, "engine_restart_during_operation"))
        self.assertEqual(e.estado, "requiere_intervencion")
        sid = self.cli.session_id
        self.admin._call("POST", f"/sessions/{sid}/resolve")          # solo una persona (admin)
        time.sleep(0.6)
        self.assertFalse(self.s.latir().requiere_persona)

    def test_forms_tambien_se_reinicia_y_pierde_el_id(self):
        self.s.iniciar()
        self.s.latir(operation="in_progress")
        self.reiniciar_motor()
        nuevo = SesionDiagnostico(CauceSessionClient(self.tok["token"], port=self.port), "forms-lab", conexion="sim-a")
        e = nuevo.iniciar()                           # Forms arrancó de cero: registra otra vez
        self.assertEqual((e.requiere_persona, e.motivo), (True, "engine_restart_during_operation"),
                         "el nuevo registro hereda la marca: reiniciar Forms no la borra")

    def test_sin_persistencia_el_404_lleva_a_registrar_de_nuevo(self):
        self.s.iniciar()
        old = self.cli.session_id
        self.reiniciar_motor(persist=False)
        e = self.s.latir()
        self.assertNotEqual(self.cli.session_id, old)
        self.assertEqual(e.estado, "iniciada")

    def test_credencial_revocada_deja_de_latir(self):
        self.s.iniciar()
        self.admin.revoke_token(self.tok["id"])
        time.sleep(0.6)
        e = self.s.latir()
        self.assertEqual(e.estado, CREDENCIAL_INVALIDA)
        n = len(self.cli.enviados)
        self.s.latir()
        self.assertEqual(len(self.cli.enviados), n, "tras un 401 no se insiste")

    def test_latidos_demasiado_seguidos_no_insisten(self):
        self.s.iniciar()
        self.s.latir()
        e = self.s.latir()                            # < 0,5 s: 429
        self.assertEqual(e.estado, "demasiado_frecuente")
        n = len(self.cli.enviados)
        self.s.latir()
        self.assertEqual(len(self.cli.enviados), n, "espera antes de volver a intentarlo")

    def test_marca_de_persona_no_se_olvida_si_el_motor_cae(self):
        self.s.iniciar()
        self.s.latir(operation="in_progress")
        self.reiniciar_motor()
        time.sleep(0.6)
        self.assertTrue(self.s.latir().requiere_persona)
        self.apagar_motor()
        time.sleep(0.6)
        e = self.s.latir()
        self.assertEqual((e.estado, e.requiere_persona), (SIN_INFORMACION, True))


if __name__ == "__main__":
    unittest.main()
