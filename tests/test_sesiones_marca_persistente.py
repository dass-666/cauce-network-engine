"""E6: una marca de «requiere intervención humana» no puede desaparecer por caducidad, por cerrar y volver a registrar,
por usar el respaldo (más antiguo) del archivo de estado, por un archivo ilegible ni por recuperar la conectividad.
Solo una persona (resolve, credencial admin) la quita."""
import json
import os
import unittest

from test_sesiones_persistencia import Reg
from cauce.session_store import SessionStore


def marcar(h, owner="tok-a", label="forms-a"):
    """Sesión con una operación interrumpida por un reinicio del motor (queda requires_human)."""
    s = h.reg(owner, label)
    h.beat(s.id, owner, operation="in_progress")
    r = h.restart()
    s = r.all()[0]
    assert s.requires_human
    return s


class MarcaPersistenteTest(unittest.TestCase):
    def test_caducada_con_marca_el_nuevo_registro_la_hereda(self):
        h = Reg()
        marcar(h)
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        self.assertEqual(h.r.all(), [])
        n = h.reg()
        self.assertTrue(n.requires_human, "la caducidad no puede dar por revisada una operación incierta")

    def test_caducada_con_marca_se_hereda_aunque_el_motor_reinicie(self):
        h = Reg()
        marcar(h)
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        h.restart()
        self.assertTrue(h.reg().requires_human)

    def test_cerrar_y_volver_a_registrar_no_borra_la_marca(self):
        h = Reg()
        s = marcar(h)
        h.beat(s.id)                                    # deja de estar «restaurada»
        h.r.close(s.id, "tok-a")
        self.assertTrue(h.reg().requires_human, "el dueño no puede autoliberarse cerrando y registrando otra")

    def test_otra_credencial_con_la_misma_etiqueta_hereda_la_marca(self):
        h = Reg()
        s = marcar(h)
        h.beat(s.id)
        h.r.close(s.id, "tok-a")
        self.assertTrue(h.reg(owner="tok-nueva").requires_human)

    def test_resolver_quita_la_marca_y_ya_no_se_hereda(self):
        h = Reg()
        s = marcar(h)
        h.beat(s.id)
        h.r.close(s.id, "tok-a")
        n = h.reg()
        h.r.resolve(n.id)
        h.r.close(n.id, "tok-a")
        self.assertFalse(h.reg().requires_human)

    def test_respaldo_mas_antiguo_que_la_marca_no_la_pierde(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="idle")                   # .bak quedará SIN marca
        h.clock.advance(1)
        h.r.evaluate()
        h.beat(s.id, operation="in_progress")            # principal: operación en curso
        with open(h.path, "w") as fh:                    # el principal se daña
            fh.write("basura")
        r = h.restart()                                  # se usa el respaldo, que no sabía de la operación
        self.assertTrue(r.all()[0].requires_human, "con el respaldo no se sabe qué pasó después: pide persona")

    def test_archivo_ilegible_sin_respaldo_obliga_a_revisar(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        for p in (h.path, h.path + ".bak"):
            with open(p, "w") as fh:
                fh.write("basura")
        r = h.restart()
        self.assertEqual(r.all(), [])
        self.assertTrue(h.reg().requires_human, "si no se puede leer el estado anterior, nada se da por bueno")

    def test_recuperar_la_red_no_quita_ninguna_de_estas_marcas(self):
        h = Reg(net="down")
        marcar(h)
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        h.net["v"] = "ok"
        n = h.reg()
        h.beat(n.id, operation="idle")
        h.r.evaluate()
        self.assertTrue(h.r.get(n.id, "tok-a").requires_human)

    def test_archivo_de_version_anterior_sigue_leyendose(self):
        """Compatibilidad: un archivo guardado por d4b17c9 (sin el campo nuevo) se lee y conserva su marca."""
        h = Reg()
        marcar(h)
        doc = json.load(open(h.path))
        doc.pop("pending", None)
        body = {k: doc[k] for k in ("schema", "saved_at", "sessions", "closed")}
        from cauce.session_store import _digest
        doc = {**body, "sha256": _digest(body)}
        json.dump(doc, open(h.path, "w"))
        os.remove(h.path + ".bak")
        doc2, st = SessionStore(h.path).load()
        self.assertEqual(st, "ok")
        self.assertTrue(h.restart().all()[0].requires_human)


if __name__ == "__main__":
    unittest.main()
