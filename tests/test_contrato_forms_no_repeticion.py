"""E6: el consumidor de referencia no puede repetir ni reenviar una acción de la persona, y una acción que falla a
medias (o que empieza justo después de un latido) siempre acaba en «revisar a mano». Motor real, red simulada."""
import time
import unittest

from test_contrato_forms import ContratoBase
from cauce.client import CauceSessionClient
from cauce.session_consumer import SesionDiagnostico


class Fallo(Exception):
    pass


class NoRepeticionTest(ContratoBase):
    def vista_admin(self):
        return self.admin._call("GET", f"/sessions/{self.cli.session_id}")

    def test_accion_que_falla_a_medias_queda_para_revisar(self):
        self.s.iniciar()
        acciones = []
        with self.assertRaises(Fallo):                      # el error llega a Forms tal cual: no se traga ni se reintenta
            with self.s.operacion():
                acciones.append("enviar")
                raise Fallo("la página dejó de responder tras pulsar Enviar")
        self.assertEqual(acciones, ["enviar"])
        self.assertTrue(self.s.ultimo.requiere_persona, "Forms debe mostrar «revisar a mano»")
        v = self.vista_admin()
        self.assertTrue(v["requires_human"], v)
        self.assertEqual(v["human_reason"], "operation_interrupted")
        time.sleep(0.6)
        self.assertTrue(self.s.latir().requiere_persona, "un latido sano posterior no quita la marca")

    def test_operacion_justo_despues_de_un_latido_se_marca_igual(self):
        """Un latido <0,5 s antes hacía que «en curso» recibiera 429 y no se enviara: un reinicio durante el envío
        no dejaba marca (demostrado en E6)."""
        self.s.iniciar()
        self.s.latir()
        with self.s.operacion():
            self.reiniciar_motor()
        self.assertTrue(self.s.ultimo.requiere_persona, self.s.ultimo)
        self.assertTrue(self.vista_admin()["requires_human"])

    def test_motor_caido_y_accion_fallida_dice_revisar_aunque_no_pueda_avisar(self):
        self.s.iniciar()
        self.apagar_motor()
        with self.assertRaises(Fallo):
            with self.s.operacion():
                raise Fallo("x")
        self.assertTrue(self.s.ultimo.requiere_persona)
        self.assertGreaterEqual(self.s.marcas_no_enviadas, 1)

    def test_la_accion_se_ejecuta_una_sola_vez_ante_cualquier_respuesta_del_motor(self):
        escenarios = {
            "normal": lambda: None,
            "motor apagado": self.apagar_motor,
            "motor reiniciado": self.reiniciar_motor,
            "credencial revocada": lambda: self.admin.revoke_token(self.tok["id"]),
        }
        for nombre, perturbar in escenarios.items():
            with self.subTest(nombre):
                self.tearDown(); self.setUp()
                self.s.iniciar()
                n = []
                with self.s.operacion():
                    perturbar()
                    n.append(1)
                self.assertEqual(len(n), 1, nombre)
                self.assertFalse(self.s.ultimo.puede_reintentar_automaticamente)

    def test_el_consumidor_no_tiene_forma_de_quitar_la_marca(self):
        for obj in (SesionDiagnostico, CauceSessionClient):
            self.assertFalse([m for m in dir(obj) if "resolv" in m.lower()], obj)
        self.s.iniciar()
        st = None
        try:
            self.cli._c._call("POST", f"/sessions/{self.cli.session_id}/resolve")
        except Exception as e:                               # noqa: BLE001
            st = getattr(e, "status", None)
        self.assertIn(st, (401, 403), "una credencial session no puede resolver")


if __name__ == "__main__":
    unittest.main()
