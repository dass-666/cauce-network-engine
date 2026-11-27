"""E7 (rama independiente, NO RC2): ver y resolver marcas «revisar a mano» cuya sesión ya no existe.

Requisitos de la misión: visualización, resolución solo admin, ids inequívocos, motivo y resolución registrados,
auditoría obligatoria, protección contra resolución accidental, concurrencia y persistencia tras reinicios.
"""
import json
import os
import threading
import time
import unittest

from helpers import sim_engine
from test_sesiones_marca_persistente import marcar
from test_sesiones_persistencia import Reg
from cauce.api import CauceAPI
from cauce.client import CauceAPIError, CauceClient, CauceSessionClient
from cauce.sessions import SessionError


def suelta(h, owner="tok-a", label="forms-a"):
    """Marca que queda sin sesión: operación interrumpida y la sesión caduca."""
    marcar(h, owner, label)
    h.clock.advance(5 * 3600)
    h.r.evaluate()
    assert h.r.all() == []
    p = h.r.pending()
    assert len(p) == 1, p
    return p[0]


class RegistroTest(unittest.TestCase):
    def test_id_inequivoco_y_estable_tras_reinicios(self):
        h = Reg()
        a = suelta(h, "tok-a", "forms-a")
        self.assertRegex(a["id"], r"^pm_[0-9a-f]{12}$")
        self.assertEqual((a["kind"], a["label"], a["confirm_with"]), ("session", "forms-a", "forms-a"))
        r2 = h.restart()
        self.assertEqual([p["id"] for p in r2.pending()], [a["id"]])
        r3 = h.restart()
        self.assertEqual([p["id"] for p in r3.pending()], [a["id"]])

    def test_dos_marcas_distintas_tienen_ids_distintos(self):
        h = Reg()
        marcar(h, "tok-a", "forms-a")
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        marcar(h, "tok-b", "forms-b")
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        ids = [p["id"] for p in h.r.pending()]
        self.assertGreaterEqual(len(ids), 2)
        self.assertEqual(len(ids), len(set(ids)))

    def test_archivo_anterior_sin_id_recibe_uno_estable(self):
        h = Reg()
        p = suelta(h)
        with open(h.path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for x in doc["pending"]:
            x.pop("id", None)
        # se reescribe como un archivo de E6 (sin id); el almacén recalcula el resumen al guardar
        h.r.store._save(doc["sessions"], doc["closed"], doc["saved_at"], doc["pending"])
        r2 = h.restart()
        self.assertEqual([x["id"] for x in r2.pending()], [p["id"]])

    def test_nota_y_confirmacion_obligatorias(self):
        h = Reg()
        p = suelta(h)
        for body, code in (({}, "note_required"), ({"note": "ok"}, "note_required"),
                           ({"note": "x" * 501, "confirm": "forms-a"}, "note_too_long"),
                           ({"note": "revisado en el portal"}, "confirmation_mismatch"),
                           ({"note": "revisado en el portal", "confirm": "forms-b"}, "confirmation_mismatch"),
                           ({"note": "revisado en el portal", "confirm": "GENERAL"}, "confirmation_mismatch")):
            with self.assertRaises(SessionError) as cm:
                h.r.resolve_pending(p["id"], body)
            self.assertEqual(cm.exception.code, code, body)
        self.assertEqual(len(h.r.pending()), 1, "un intento fallido no puede quitar la marca")
        self.assertNotIn("session.pending_resolved", h.kinds())

    def test_resolver_registra_motivo_nota_y_persiste(self):
        h = Reg()
        p = suelta(h)
        out = h.r.resolve_pending(p["id"], {"note": "Folio comprobado en el portal", "confirm": "forms-a"},
                                  resolved_by="operator")
        self.assertEqual(out["resolved"], p["id"])
        ev = [e for e in h.audit.events(limit=5000) if e["kind"] == "session.pending_resolved"]
        self.assertEqual(len(ev), 1)
        d0 = ev[0]["data"]
        self.assertEqual((d0["pending_id"], d0["reason"], d0["note"], d0["resolved_by"]),
                         (p["id"], p["reason"], "Folio comprobado en el portal", "operator"))
        r2 = h.restart()
        self.assertEqual(r2.pending(), [], "tras reiniciar, la marca resuelta no puede volver")
        n = h.reg()
        self.assertFalse(n.requires_human, "ya no se hereda en el siguiente registro")

    def test_resolver_dos_veces_da_404(self):
        h = Reg()
        p = suelta(h)
        h.r.resolve_pending(p["id"], {"note": "revisado en el portal", "confirm": "forms-a"})
        with self.assertRaises(SessionError) as cm:
            h.r.resolve_pending(p["id"], {"note": "revisado en el portal", "confirm": "forms-a"})
        self.assertEqual(cm.exception.status, 404)

    def test_marca_general_se_confirma_con_GENERAL(self):
        h = Reg()
        with open(h.path, "w") as fh:
            fh.write("{dañado")
        r = h.restart()
        p = r.pending()
        self.assertEqual([(x["kind"], x["confirm_with"]) for x in p], [("general", "GENERAL")])
        r.resolve_pending(p[0]["id"], {"note": "estado revisado a mano", "confirm": "GENERAL"})
        self.assertEqual(r.pending(), [])
        self.assertFalse(h.reg().requires_human)

    def test_concurrencia_solo_una_resolucion_gana(self):
        h = Reg()
        p = suelta(h)
        res, errs = [], []
        barrera = threading.Barrier(8)

        def intento():
            barrera.wait()
            try:
                res.append(h.r.resolve_pending(p["id"], {"note": "revisado en el portal", "confirm": "forms-a"}))
            except SessionError as e:
                errs.append(e.status)

        ts = [threading.Thread(target=intento) for _ in range(8)]
        [t.start() for t in ts]
        [t.join(5) for t in ts]
        self.assertEqual((len(res), sorted(errs)), (1, [404] * 7))
        self.assertEqual(h.kinds().count("session.pending_resolved"), 1)

    def test_resolver_una_no_toca_la_otra(self):
        h = Reg()
        marcar(h, "tok-a", "forms-a")
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        marcar(h, "tok-b", "forms-b")
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        ps = {p["label"]: p for p in h.r.pending()}
        h.r.resolve_pending(ps["forms-a"]["id"], {"note": "revisado en el portal", "confirm": "forms-a"})
        self.assertEqual([p["label"] for p in h.r.pending()], ["forms-b"])
        r2 = h.restart()
        self.assertEqual([p["id"] for p in r2.pending()], [ps["forms-b"]["id"]])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine(sessions={"max": 4})
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.admin = CauceClient(self.api.token, port=self.api.port)
        self.ts = self.admin.create_token("forms-a", "session")
        self.tr = self.admin.create_token("monitor", "read")

    def tearDown(self):
        self.api.stop()

    def _marca_suelta(self):
        a = CauceSessionClient(self.ts["token"], port=self.api.port)
        a.register("forms-a")
        time.sleep(0.6)
        a.heartbeat(ok=False, error_code="other", operation="in_progress")
        a.close()                                # se cierra con la marca: queda pendiente sin sesión
        p = self.admin.pending_reviews()
        self.assertEqual(len(p), 1)
        return p[0]

    def test_solo_admin_ve_y_resuelve(self):
        p = self._marca_suelta()
        for tok in (self.ts["token"], self.tr["token"]):
            c = CauceClient(tok, port=self.api.port)
            with self.assertRaises(CauceAPIError) as cm:
                c.pending_reviews()
            self.assertEqual(cm.exception.status, 403)
            with self.assertRaises(CauceAPIError) as cm:
                c.resolve_pending(p["id"], "revisado en el portal", "forms-a")
            self.assertEqual(cm.exception.status, 403)
        self.assertEqual(len(self.admin.pending_reviews()), 1)
        # la vista de lectura (Monitor) la ve con su id, sin poder resolverla
        st = CauceClient(self.tr["token"], port=self.api.port)._call("GET", "/status/sessions")
        self.assertEqual([x["id"] for x in st["pending_human_review"]], [p["id"]])
        out = self.admin.resolve_pending(p["id"], "Folio comprobado en el portal", "forms-a")
        self.assertEqual(out["resolved"], p["id"])
        self.assertEqual(self.admin.pending_reviews(), [])

    def test_confirmacion_equivocada_es_409_y_no_cambia_nada(self):
        p = self._marca_suelta()
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.resolve_pending(p["id"], "revisado en el portal", "forms-b")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "confirmation_mismatch"))
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.resolve_pending("pm_000000000000", "revisado en el portal", "forms-a")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(self.admin.pending_reviews()), 1)


if __name__ == "__main__":
    unittest.main()
