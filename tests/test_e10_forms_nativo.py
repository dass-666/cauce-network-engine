"""E10 (rama independiente; RC2 y RC3 no cambian): solicitudes S1–S3 de Forms nativo en Network Engine.

S1  recuperar la sesión propia cuando Forms pierde su id, SIN que una segunda instancia desplace una sesión viva.
S2  resolver una marca cuya condición sigue presente → 409 condition_persists (antes reaparecía al instante).
S3  el dueño conoce las revisiones administrativas de sus marcas; nunca equivalen a un acuse (confirms_receipt false).
"""
import json
import os
import threading
import unittest

from test_sesiones_persistencia import Reg
from test_sessions import SessionApiBase
from cauce.client import CauceAPIError, CauceSessionClient
from cauce.session_store import ReviewStore, SessionStore, _digest
from cauce.sessions import SessionError, SessionRegistry


class RegR(Reg):
    """Registro con almacén de sesiones Y de revisiones reales (reinicio incluido)."""

    def _new(self, store=None):
        return SessionRegistry(self.audit, lambda cid: self.net["v"], lambda cid: True, clock=self.clock,
                               store=store or SessionStore(self.path), restore_max_age_s=self.max_age,
                               review_store=ReviewStore(os.path.join(self.d, "sessions_reviews.json")))


def _err(fn):
    try:
        fn()
    except SessionError as e:
        return e
    raise AssertionError("se esperaba SessionError")


# --------------------------------------------------------------------------------------------------- S1
class S1RecuperarSesionPropiaTest(unittest.TestCase):
    def test_segunda_instancia_no_desplaza_una_sesion_viva(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="idle")
        h.clock.advance(10)                                   # 10 s < 30 s de plazo: la anterior sigue viva
        e = _err(lambda: h.reg())
        self.assertEqual((e.status, e.code), (409, "credential_has_session"))
        self.assertAlmostEqual(e.details["retry_after_s"], 20.0, places=1)
        self.assertEqual([x.id for x in h.r.all()], [s.id])   # nada cambió
        h.beat(s.id)                                          # y la viva sigue latiendo con normalidad

    def test_tras_su_plazo_en_silencio_el_dueno_recupera_y_hereda_la_operacion(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="in_progress")                 # la instancia anterior calla con una operación a medias
        h.clock.advance(31)
        n = h.reg()
        self.assertNotEqual(n.id, s.id)
        self.assertTrue(n.requires_human)
        self.assertEqual(n.human_reason in ("owner_replaced_during_operation", "operation_interrupted"), True)
        cerrada = next(c for c in h.r.summary()["closed"] if c["id"] == s.id)
        self.assertEqual(cerrada["reason"], "replaced_by_owner")
        self.assertIn("session.replaced", h.kinds())
        # el id antiguo ya no sirve: la instancia anterior, si despierta, recibe 404 y al re-registrarse 409
        e = _err(lambda: h.beat(s.id))
        self.assertEqual(e.code, "unknown_session")
        e = _err(lambda: h.reg())
        self.assertEqual(e.code, "credential_has_session")

    def test_sin_operacion_en_curso_no_inventa_marca(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="idle")
        h.clock.advance(31)
        n = h.reg()
        self.assertFalse(n.requires_human)

    def test_marca_previa_se_hereda_siempre(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, status="error", error_code="session_expired")
        self.assertTrue(h.r.get(s.id, "tok-a").requires_human)
        h.clock.advance(31)
        n = h.reg()
        self.assertEqual((n.requires_human, n.human_reason), (True, "session_expired"))

    def test_otra_credencial_nunca_sustituye(self):
        h = RegR()
        s = h.reg("tok-a", "forms-a")
        h.clock.advance(3600 - 1)                             # muy silenciosa, pero de OTRA credencial
        e = _err(lambda: h.reg("tok-b", "forms-a"))
        self.assertEqual(e.code, "label_in_use")
        self.assertEqual([x.id for x in h.r.all()], [s.id])

    def test_concurrencia_solo_una_instancia_gana(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(31)
        res, barrera = [], threading.Barrier(6)

        def intento():
            barrera.wait()
            try:
                res.append(("ok", h.reg().id))
            except SessionError as e:
                res.append((e.code, None))
        hs = [threading.Thread(target=intento) for _ in range(6)]
        [t.start() for t in hs]
        [t.join() for t in hs]
        ganadores = [r for r in res if r[0] == "ok"]
        self.assertEqual(len(ganadores), 1, res)
        self.assertTrue(all(c == "credential_has_session" for c, _ in res if c != "ok"), res)
        self.assertEqual(len(h.r.all()), 1)
        self.assertTrue(h.r.all()[0].requires_human)

    def test_persistencia_de_la_sustitucion(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(31)
        n = h.reg()
        r = h.restart()
        v = r.view(r.all()[0])
        self.assertEqual((v["id"], v["requires_human"]), (n.id, True))
        # el formato de sessions_state.json es el de RC3 (mismas claves en el resumen): volver a RC3 lo lee
        doc = json.load(open(h.path))
        self.assertEqual(set(doc) - {"sha256"}, {"schema", "saved_at", "sessions", "closed", "pending"})
        self.assertEqual(doc["sha256"], _digest({k: doc[k] for k in ("schema", "saved_at", "sessions", "closed", "pending")}))


class S1ApiTest(SessionApiBase):
    def test_409_lleva_retry_after_y_credencial_revocada_no_sustituye(self):
        self.a.register("forms-a", heartbeat_timeout_s=10)
        otra = CauceSessionClient(self.tok_a["token"], port=self.api.port)
        st, data = self.raw("POST", "/api/v1/sessions", self.tok_a["token"],
                            {"label": "forms-a", "authorized_by_user": True, "heartbeat_timeout_s": 10})
        data = json.loads(data)
        self.assertEqual(st, 409)
        self.assertIn("retry_after_s", data["error"]["details"])
        self.clock.advance(11)
        otra.register("forms-a", heartbeat_timeout_s=10)       # el dueño recupera tras el silencio
        self.assertNotEqual(otra.session_id, self.a.session_id)
        # credencial revocada: 401 antes de tocar nada
        self.admin.revoke_token(self.tok_a["id"])
        self.clock.advance(11)
        with self.assertRaises(CauceAPIError) as cm:
            CauceSessionClient(self.tok_a["token"], port=self.api.port).register("forms-a")
        self.assertEqual(cm.exception.status, 401)
        # E10: revocar cierra su sesión (sin operación en curso: no deja marca)
        self.assertEqual(self.eng.sessions.all(), [])


# --------------------------------------------------------------------------------------------------- S2
class S2ResolverTest(unittest.TestCase):
    def test_condicion_persistente_409_y_la_marca_no_se_toca(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, status="error", error_code="browser_closed", operation="in_progress")
        self.assertTrue(h.r.get(s.id, "tok-a").requires_human)
        e = _err(lambda: h.r.resolve(s.id))
        self.assertEqual((e.status, e.code), (409, "condition_persists"))
        self.assertEqual(e.details["condition"], "operation_still_reported_in_progress")
        h.r.evaluate()
        self.assertTrue(h.r.get(s.id, "tok-a").requires_human)       # sigue: no se debilitó nada
        self.assertNotIn("session.human_resolved", h.kinds())

    def test_cuando_el_dueno_informa_el_final_se_resuelve_y_no_reaparece(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, status="error", error_code="browser_closed", operation="in_progress")
        h.beat(s.id, status="error", error_code="browser_closed", operation="idle")
        h.r.resolve(s.id, {"note": "comprobado en el portal propio"})
        for _ in range(3):
            h.clock.advance(1)
            h.r.evaluate()
            self.assertFalse(h.r.get(s.id, "tok-a").requires_human)

    def test_revision_local_pendiente_del_puesto(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, local_review="pending")
        v = h.r.view(h.r.get(s.id, "tok-a"))
        self.assertEqual((v["requires_human"], v["human_reason"], v["local_review"]),
                         (True, "owner_local_review_pending", "pending"))
        e = _err(lambda: h.r.resolve(s.id))
        self.assertEqual(e.details["condition"], "owner_local_review_pending")
        self.assertIn("no confirma", e.message)
        h.beat(s.id, local_review="none")                    # el puesto revisó lo suyo
        self.assertTrue(h.r.get(s.id, "tok-a").requires_human)   # la marca sigue hasta que el operador la resuelva
        h.r.resolve(s.id)
        self.assertFalse(h.r.get(s.id, "tok-a").requires_human)

    def test_local_review_invalido(self):
        h = RegR()
        s = h.reg()
        e = _err(lambda: h.beat(s.id, local_review="ok"))
        self.assertEqual(e.code, "invalid_request")

    def test_nota_demasiado_larga(self):
        h = RegR()
        s = h.reg()
        e = _err(lambda: h.r.resolve(s.id, {"note": "x" * 501}))
        self.assertEqual(e.status, 400)


class S2ApiTest(SessionApiBase):
    def test_api_409_con_detalle(self):
        self.a.register("forms-a")
        self.clock.advance(1)
        self.a.heartbeat(False, "browser_closed", operation="in_progress")
        st, data = self.raw("POST", f"/api/v1/sessions/{self.a.session_id}/resolve", self.api.token, {})
        data = json.loads(data)
        self.assertEqual((st, data["error"]["code"]), (409, "condition_persists"))
        self.assertEqual(data["error"]["details"]["requires_human"], True)
        self.assertNotIn(self.tok_a["token"], json.dumps(data))


# --------------------------------------------------------------------------------------------------- S3
class S3RevisionesTest(unittest.TestCase):
    def test_el_dueno_ve_la_revision_sin_nota_y_sin_valor_de_acuse(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, status="error", error_code="session_expired")
        h.r.resolve(s.id, {"note": "Juan: revisé la sesión, DATO-SECRETO"}, resolved_by="operador")
        out = h.r.reviews_for(s.id, "tok-a")
        self.assertEqual(len(out["reviews"]), 1)
        r = out["reviews"][0]
        self.assertEqual((r["kind"], r["reason"], r["confirms_receipt"], r["evidence"]),
                         ("session_resolve", "session_expired", False, "none"))
        self.assertNotIn("DATO-SECRETO", json.dumps(out))
        self.assertNotIn("tok-a", json.dumps(out))
        self.assertIn("NO confirma", out["notice"])

    def test_revision_de_marca_pendiente_visible_para_la_nueva_sesion_de_la_misma_etiqueta(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(5 * 3600)
        h.r.evaluate()                                       # caduca: la marca queda pendiente
        p = h.r.pending()[0]
        h.r.resolve_pending(p["id"], {"note": "revisado en Monitor", "confirm": "forms-a"}, resolved_by="monitor")
        n = h.reg()                                          # el puesto vuelve
        self.assertFalse(n.requires_human)
        out = h.r.reviews_for(n.id, "tok-a")
        self.assertEqual([(x["kind"], x["pending_id"], x["confirms_receipt"]) for x in out["reviews"]],
                         [("pending_resolve", p["id"], False)])

    def test_otro_dueno_no_ve_revisiones_ajenas(self):
        h = RegR()
        a = h.reg("tok-a", "forms-a")
        b = h.reg("tok-b", "forms-b")
        h.beat(a.id, "tok-a", status="error", error_code="session_expired")
        h.r.resolve(a.id)
        self.assertEqual(h.r.reviews_for(b.id, "tok-b")["reviews"], [])
        e = _err(lambda: h.r.reviews_for(a.id, "tok-b"))
        self.assertEqual(e.code, "not_session_owner")

    def test_revisiones_sobreviven_al_reinicio_y_un_archivo_danado_no_rompe_nada(self):
        h = RegR()
        s = h.reg()
        h.beat(s.id, status="error", error_code="session_expired")
        h.r.resolve(s.id)
        r = h.restart()
        h.clock.advance(1)
        r.heartbeat(s.id, "tok-a", {"status": "ok"})
        self.assertEqual(len(r.reviews_for(s.id, "tok-a")["reviews"]), 1)
        for f in ("sessions_reviews.json", "sessions_reviews.json.bak"):
            p = os.path.join(h.d, f)
            if os.path.exists(p):
                open(p, "w").write("{dañado")
        r = h.restart()
        self.assertEqual(len(r.all()), 1)                     # las sesiones no se ven afectadas
        self.assertEqual(r.all()[0].id, s.id)


class S3ApiTest(SessionApiBase):
    def test_rutas_y_permisos(self):
        self.a.register("forms-a")
        self.clock.advance(1)
        self.a.heartbeat(False, "session_expired")
        self.admin._call("POST", f"/sessions/{self.a.session_id}/resolve", {"note": "revisado"})
        st, data = self.raw("GET", f"/api/v1/sessions/{self.a.session_id}/reviews", self.tok_a["token"])
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(data)["reviews"][0]["confirms_receipt"], False)
        for tok in (self.read_token, self.tok_b["token"]):
            st, _ = self.raw("GET", f"/api/v1/sessions/{self.a.session_id}/reviews", tok)
            self.assertEqual(st, 403)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------------- hallado en las jornadas E10: revocación
class RevocacionCierraSesionTest(SessionApiBase):
    def test_revocar_cierra_la_sesion_libera_la_etiqueta_y_conserva_la_marca(self):
        self.a.register("forms-lab-jornada")
        self.clock.advance(1)
        self.a.heartbeat(True, operation="in_progress")           # revocada a mitad de una operación
        out = self.admin.revoke_token(self.tok_a["id"])
        self.assertEqual(out.get("sessions_closed"), 1)
        self.assertEqual(self.eng.sessions.all(), [])
        pend = self.eng.sessions.pending()
        self.assertEqual([(p["label"], p["reason"]) for p in pend],
                         [("forms-lab-jornada", "credential_revoked_during_operation")])
        # el administrador crea una credencial NUEVA para el mismo puesto: registra con la misma etiqueta y hereda
        nueva = self.admin.create_token("forms-a", "session")
        c = CauceSessionClient(nueva["token"], port=self.api.port)
        v = c.register("forms-lab-jornada")
        self.assertEqual((v["requires_human"], v["human_reason"]), (True, "credential_revoked_during_operation"))
        # el id antiguo ya no existe: 404 (no 403), así un cliente sabe que debe registrarse de nuevo
        st, _ = self.raw("POST", f"/api/v1/sessions/{self.a.session_id}/heartbeat", nueva["token"], {"status": "ok"})
        self.assertEqual(st, 404)
