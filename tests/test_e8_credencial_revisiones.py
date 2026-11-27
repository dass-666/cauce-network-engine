"""E8: credencial «review» de mínimo privilegio para el centro de revisiones de Monitor.

Solo ve y resuelve revisiones pendientes. Se recorren las rutas REALES del servidor: todo lo demás le da 403."""
import re
import time

from test_read_tokens import Base
from cauce.client import CauceClient, CauceSessionClient


class CredencialRevisionTest(Base):
    REVIEW_ROUTES = {("GET", "/api/v1/sessions/pending"), ("POST", "/api/v1/sessions/pending/pm_0123456789ab/resolve")}

    def setUp(self):
        super().setUp()
        rec = self.admin.create_token("monitor-revisiones", "review")
        self.rev_token, self.rev_id = rec["token"], rec["id"]

    def _marca(self):
        t = self.admin.create_token("forms-a", "session")
        c = CauceSessionClient(t["token"], port=self.api.port)
        c.register("forms-a")
        time.sleep(0.6)
        c.heartbeat(ok=False, error_code="network_error", operation="in_progress")
        c.close()
        return self.admin.pending_reviews()[0]

    def test_review_solo_llega_a_sus_dos_rutas(self):
        permitidas, negadas = set(), 0
        for method, pat, _fn, need in self.api.routes:
            raw = pat.pattern[1:-1]

            def fill(m):
                return next(c for c in ("sim-a", "00000000", "ls_abc", "tk_abc", "bs_0123456789ab", "pm_0123456789ab",
                                        "activate", "r", "1") if re.fullmatch(m.group(1), c))
            path = re.sub(r"\(\?P<\w+>((?:[^()]|\([^()]*\))*)\)", fill, raw)
            if path == "/api/v1/ping":
                continue
            if need == "review":
                permitidas.add((method, path))
                continue
            st, data = self.raw(method, path, self.rev_token, {} if method != "GET" else None)
            self.assertEqual((st, self.code(data)), (403, "insufficient_scope"), f"{method} {path}")
            negadas += 1
        self.assertEqual(permitidas, self.REVIEW_ROUTES)
        self.assertGreaterEqual(negadas, 25)

    def test_read_y_session_no_resuelven(self):
        p = self._marca()
        ses = self.admin.create_token("forms-b", "session")["token"]
        for tk in (self.read_token, ses):
            st, data = self.raw("POST", f"/api/v1/sessions/pending/{p['id']}/resolve", tk,
                                {"note": "intento sin permiso", "confirm": p["confirm_with"]})
            self.assertEqual((st, self.code(data)), (403, "insufficient_scope"))
        self.assertEqual(len(self.admin.pending_reviews()), 1)

    def test_review_resuelve_con_nota_y_queda_auditado_con_su_nombre(self):
        p = self._marca()
        rev = CauceClient(self.rev_token, port=self.api.port)
        self.assertEqual([x["id"] for x in rev.pending_reviews()], [p["id"]])
        rev.resolve_pending(p["id"], "revisado en el portal: no se envió", p["confirm_with"])
        self.assertEqual(self.admin.pending_reviews(), [])
        ev = [e for e in self.eng.audit.events(limit=2000) if e["kind"] == "session.pending_resolved"][-1]
        self.assertEqual((ev["data"]["resolved_by"], ev["data"]["note"]),
                         ("monitor-revisiones", "revisado en el portal: no se envió"))

    def test_review_no_crea_credenciales_ni_lee_la_red(self):
        for method, path, body in (("POST", "/api/v1/tokens", {"name": "x", "scope": "review"}),
                                   ("GET", "/api/v1/status/connectivity", None), ("POST", "/api/v1/shutdown", {})):
            st, data = self.raw(method, path, self.rev_token, body)
            self.assertEqual(st, 403, f"{method} {path}")

    def test_revocar_review_la_deja_sin_acceso(self):
        self.admin.revoke_token(self.rev_id)
        st, _ = self.raw("GET", "/api/v1/sessions/pending", self.rev_token)
        self.assertEqual(st, 401)
