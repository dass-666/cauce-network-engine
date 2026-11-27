"""E7: hallazgos de la auditoría independiente. Cada prueba reproduce un defecto demostrado en RC2 (6e2cceb)."""
import threading
import unittest

from test_sesiones_persistencia import Reg
from cauce.session_store import SessionStore
from cauce.sessions import SessionError


class GuardadoConcurrenteTest(unittest.TestCase):
    def test_un_guardado_viejo_no_pisa_la_marca_en_curso(self):
        """Dos hilos guardan a la vez (servidor HTTP con hilos + bucle del motor). Si el que tomó la foto ANTES escribe
        DESPUÉS, el disco vuelve a «idle» y un apagado en ese hueco borra la operación en curso."""
        r = Reg()
        s = r.reg()
        r.beat(s.id)
        entra, sigue = threading.Event(), threading.Event()
        real = SessionStore.save
        primera = [True]

        def lenta(self_, *a, **kw):
            if primera[0] and threading.current_thread().name == "viejo":
                primera[0] = False
                entra.set()
                sigue.wait(5)
            return real(self_, *a, **kw)

        SessionStore.save = lenta
        try:
            t = threading.Thread(target=lambda: r.r._save(force=True), name="viejo")   # foto con operation=None
            t.start()
            entra.wait(5)
            r.beat(s.id, operation="in_progress")       # foto nueva, se escribe primero
            sigue.set()
            t.join(5)
        finally:
            SessionStore.save = real
        r2 = r.restart()                                # apagado justo después
        v = r2.get(s.id, "tok-a")
        self.assertTrue(v.requires_human, f"la operación en curso se perdió en disco: operation={v.operation}")


class RegistroFallidoTest(unittest.TestCase):
    def test_un_registro_rechazado_no_borra_la_sesion_restaurada_ni_su_marca(self):
        r = Reg()
        a = r.reg("tok-a", "forms-a")
        r.reg("tok-b", "forms-b")
        r.beat(a.id, "tok-a", operation="in_progress")
        r2 = r.restart()                                 # a vuelve restaurada y pidiendo persona
        with self.assertRaises(SessionError):
            r2.register("tok-a", "forms", {"label": "forms-b", "authorized_by_user": True})   # 409 label_in_use
        vivas = {s.label: s for s in r2.all()}
        marcas = [p for p in r2._pending if p["owner_token_id"] == "tok-a"]
        self.assertTrue(("forms-a" in vivas and vivas["forms-a"].requires_human) or marcas,
                        "el 409 se llevó la sesión restaurada y su marca de intervención")


class ConsumidorCredencialRevocadaTest(unittest.TestCase):
    """E7: el consumidor de referencia olvidaba «revisar a mano» al recibir 401 y no contaba el aviso perdido."""

    def setUp(self):
        import time as _t
        from helpers import sim_engine
        from cauce.api import CauceAPI
        from cauce.client import CauceClient, CauceSessionClient
        from cauce.session_consumer import SesionDiagnostico
        self.t = _t
        self.eng, _ = sim_engine(sessions={"max": 4})
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.adm = CauceClient(self.api.token, port=self.api.port)
        self.tok = self.adm.create_token("forms-a", "session")
        self.s = SesionDiagnostico(CauceSessionClient(self.tok["token"], port=self.api.port), "forms-ventanilla-1")
        self.s.iniciar()

    def tearDown(self):
        self.api.stop()

    def test_la_marca_vista_no_se_olvida_al_revocar_la_credencial(self):
        with self.assertRaises(RuntimeError):
            with self.s.operacion():
                raise RuntimeError("clic sin evento submit")
        self.assertTrue(self.s.ultimo.requiere_persona)
        self.adm.revoke_token(self.tok["id"])
        self.t.sleep(0.6)
        e = self.s.latir()
        self.assertEqual(e.estado, "credencial_invalida")
        self.assertTrue(e.requiere_persona, "la pantalla olvidó «revisar a mano» por un 401")

    def test_revocada_a_mitad_cuenta_el_aviso_que_no_llego(self):
        hechas = []
        with self.s.operacion():
            hechas.append(1)
            self.adm.revoke_token(self.tok["id"])
        self.assertEqual(hechas, [1])
        self.assertEqual(self.s.marcas_no_enviadas, 1, "el «terminado» no llegó al motor y la pantalla no lo sabe")


if __name__ == "__main__":
    unittest.main()
