"""Seguimiento de dos sesiones de navegador autorizadas: estado, estabilidad y aislamiento.

TODO es simulación (conexiones ficticias del simulador) y sockets de loopback. No se abre ningún
navegador, no se visita ningún sitio externo y no se envía ningún formulario ni reserva.
Los "latidos" los envía la prueba en nombre de un dueño ficticio (lo que hará Cauce Forms).
"""
import itertools
import json
import threading
import unittest

from helpers import sim_engine
from test_read_tokens import SIM_SECRETS, Base
from cauce.client import CauceAPIError, CauceSessionClient
from cauce.sessions import SessionError, SessionRegistry


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self.t

    def advance(self, s):
        with self._lock:
            self.t += s


class SessionApiBase(Base):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.eng.sessions.clock = self.clock
        self.tok_a = self.admin.create_token("forms-a", "session")
        self.tok_b = self.admin.create_token("forms-b", "session")
        self.a = CauceSessionClient(self.tok_a["token"], port=self.api.port)
        self.b = CauceSessionClient(self.tok_b["token"], port=self.api.port)

    def beat(self, client, ok=True, code=None):
        self.clock.advance(1.0)
        return client.heartbeat(ok, code)

    def fail_link(self, cid):
        self.net.set_state(cid, "down")
        for _ in range(3):   # fail_threshold
            self.eng.health.check(self.eng.connection(cid))

    def recover_link(self, cid):
        self.net.set_state(cid, "up")
        for _ in range(2):   # recover_threshold
            self.eng.health.check(self.eng.connection(cid))

    def states(self):
        return {s["label"]: s["state"] for s in self.read.sessions()["sessions"]}


class PermissionsTest(SessionApiBase):
    def test_only_session_credentials_register(self):
        st, data = self.raw("POST", "/api/v1/sessions", self.read_token, {"label": "x1", "authorized_by_user": True})
        self.assertEqual((st, self.code(data)), (403, "insufficient_scope"))
        st, data = self.raw("POST", "/api/v1/sessions", self.api.token, {"label": "x1", "authorized_by_user": True})
        self.assertEqual((st, self.code(data)), (403, "session_credential_required"))
        self.assertEqual(self.eng.sessions.all(), [])

    def test_session_credential_cannot_read_status_or_admin(self):
        for path in ("/api/v1/status/connectivity", "/api/v1/status/sessions", "/api/v1/connections", "/api/v1/tokens"):
            st, data = self.raw("GET", path, self.tok_a["token"])
            self.assertEqual((st, self.code(data)), (403, "insufficient_scope"), path)

    def test_authorization_must_be_declared(self):
        st, data = self.raw("POST", "/api/v1/sessions", self.tok_a["token"], {"label": "nav-a"})
        self.assertEqual((st, self.code(data)), (400, "authorization_not_declared"))
        self.assertEqual(self.eng.sessions.all(), [])

    def test_one_session_per_credential_and_max_two(self):
        self.a.register("nav-a")
        with self.assertRaises(CauceAPIError) as cm:
            CauceSessionClient(self.tok_a["token"], port=self.api.port).register("nav-a2")
        self.assertEqual(cm.exception.code, "credential_has_session")
        self.b.register("nav-b")
        c = CauceSessionClient(self.admin.create_token("forms-c", "session")["token"], port=self.api.port)
        with self.assertRaises(CauceAPIError) as cm:
            c.register("nav-c")
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "too_many_sessions"))
        self.assertEqual(len(self.eng.sessions.all()), 2)

    def test_unknown_connection_rejected(self):
        with self.assertRaises(CauceAPIError) as cm:
            self.a.register("nav-a", connection_id="no-existe")
        self.assertEqual(cm.exception.code, "unknown_connection")

    def test_admin_can_view_and_close_but_not_heartbeat(self):
        sid = self.a.register("nav-a")["id"]
        st, _ = self.raw("GET", f"/api/v1/sessions/{sid}", self.api.token)
        self.assertEqual(st, 200)
        st, data = self.raw("POST", f"/api/v1/sessions/{sid}/heartbeat", self.api.token, {"status": "ok"})
        self.assertEqual((st, self.code(data)), (403, "session_credential_required"))
        st, _ = self.raw("DELETE", f"/api/v1/sessions/{sid}", self.api.token)
        self.assertEqual(st, 200)
        self.assertEqual(self.eng.sessions.all(), [])

    def test_revoked_credential_cannot_heartbeat(self):
        self.a.register("nav-a")
        self.admin.revoke_token(self.tok_a["id"])
        with self.assertRaises(CauceAPIError) as cm:
            self.beat(self.a)
        self.assertEqual(cm.exception.status, 401)


class IsolationTest(SessionApiBase):
    def setUp(self):
        super().setUp()
        self.sa = self.a.register("nav-a", connection_id="sim-a")["id"]
        self.sb = self.b.register("nav-b", connection_id="sim-b")["id"]
        self.beat(self.a)
        self.beat(self.b)

    def test_both_active(self):
        self.assertEqual(self.states(), {"nav-a": "active", "nav-b": "active"})
        iso = self.read.sessions()["isolation"]
        self.assertEqual(iso, {"one_credential_per_session": True, "shared_connection": False})

    def test_other_credential_cannot_touch_session(self):
        before = self.eng.sessions.get(self.sa, "", admin=True).heartbeats
        for method, path, body in (("POST", f"/api/v1/sessions/{self.sa}/heartbeat", {"status": "error", "error_code": "other"}),
                                   ("GET", f"/api/v1/sessions/{self.sa}", None),
                                   ("DELETE", f"/api/v1/sessions/{self.sa}", None)):
            st, data = self.raw(method, path, self.tok_b["token"], body)
            self.assertEqual((st, self.code(data)), (403, "not_session_owner"), f"{method} {path}")
        s = self.eng.sessions.get(self.sa, "", admin=True)   # EFECTO: nada cambió en A
        self.assertEqual((s.heartbeats, s.reported, s.state), (before, "ok", "active"))

    def test_link_failure_affects_only_dependent_session(self):
        self.fail_link("sim-a")
        self.assertEqual(self.states(), {"nav-a": "network_down", "nav-b": "active"})
        self.recover_link("sim-a")
        self.assertEqual(self.states(), {"nav-a": "active", "nav-b": "active"})

    def test_browser_error_in_one_session_does_not_affect_other(self):
        self.beat(self.a, ok=False, code="page_unreachable")
        st = {s["label"]: s for s in self.read.sessions()["sessions"]}
        self.assertEqual((st["nav-a"]["state"], st["nav-a"]["reason"]), ("browser_error", "page_unreachable"))
        self.assertEqual(st["nav-b"]["state"], "active")
        self.beat(self.a)
        self.assertEqual(self.states()["nav-a"], "active")

    def test_stale_detection_is_per_session(self):
        self.clock.advance(20)
        self.beat(self.b)          # B sigue latiendo, A no
        self.clock.advance(15)     # A: 36 s sin latido (> 30 s)
        self.beat(self.b)
        st = {s["label"]: s for s in self.read.sessions()["sessions"]}
        self.assertEqual((st["nav-a"]["state"], st["nav-a"]["reason"]), ("stale", "heartbeat_timeout"))
        self.assertEqual(st["nav-b"]["state"], "active")
        self.beat(self.a)          # el navegador vuelve
        self.assertEqual(self.states()["nav-a"], "active")

    def test_events_visible_to_monitor_and_sanitized(self):
        self.fail_link("sim-a")
        self.recover_link("sim-a")
        evs = [e for e in self.read.events(0)["events"] if e["kind"] == "session.state" and e["session_id"] == self.sa]
        # el primer fallo degrada (health: healthy -> degraded), el tercero confirma la caída; luego recuperación
        self.assertEqual([(e["old"], e["new"]) for e in evs][-3:],
                         [("active", "network_degraded"), ("network_degraded", "network_down"), ("network_down", "active")])
        for e in evs:
            self.assertEqual(set(e), {"seq", "ts", "kind", "session_id", "old", "new", "reason"})
        stab = {s["label"]: s["stability"] for s in self.read.sessions()["sessions"]}
        self.assertGreaterEqual(stab["nav-a"]["state_changes_10m"], 3)
        self.assertLessEqual(stab["nav-b"]["state_changes_10m"], 1)   # solo pending -> active


class DataMinimizationTest(SessionApiBase):
    def test_heartbeat_rejects_page_data(self):
        sid = self.a.register("nav-a", connection_id="sim-a")["id"]
        for extra in ({"url": "https://reservas.example/x"}, {"cookies": "s=1"}, {"form": {"nombre": "x"}},
                      {"title": "Página"}):
            body = dict({"status": "ok"}, **extra)
            st, data = self.raw("POST", f"/api/v1/sessions/{sid}/heartbeat", self.tok_a["token"], body)
            self.assertEqual((st, self.code(data)), (400, "unsupported_field"), extra)
        st, data = self.raw("POST", f"/api/v1/sessions/{sid}/heartbeat", self.tok_a["token"],
                            {"status": "error", "error_code": "algo libre"})
        self.assertEqual((st, self.code(data)), (400, "invalid_error_code"))
        self.assertEqual(self.eng.sessions.get(sid, "", admin=True).heartbeats, 0)
        st, data = self.raw("POST", "/api/v1/sessions", self.tok_b["token"],
                            {"label": "nav-b", "authorized_by_user": True, "url": "https://x"})
        self.assertEqual((st, self.code(data)), (400, "unsupported_field"))

    def test_read_view_has_no_network_secrets_or_credential_ids(self):
        self.a.register("nav-a", connection_id="sim-a")
        self.beat(self.a)
        text = json.dumps(self.read.sessions()) + json.dumps(self.read.events(0))
        for s in SIM_SECRETS + [self.tok_a["id"], self.tok_a["token"], "owner_token_id"]:
            self.assertNotIn(s, text)

    def test_heartbeat_rate_limited(self):
        sid = self.a.register("nav-a")["id"]
        self.beat(self.a)
        st, data = self.raw("POST", f"/api/v1/sessions/{sid}/heartbeat", self.tok_a["token"], {"status": "ok"})
        self.assertEqual((st, self.code(data)), (429, "heartbeat_too_frequent"))


class GlobalNetworkTest(SessionApiBase):
    def test_session_without_connection_follows_global_state(self):
        self.a.register("nav-a")
        self.beat(self.a)
        self.assertEqual(self.states()["nav-a"], "active")
        self.fail_link("sim-a")
        self.assertEqual(self.states()["nav-a"], "active")      # sim-b sigue sana
        self.fail_link("sim-b")
        s = self.read.sessions()["sessions"][0]
        self.assertEqual((s["state"], s["reason"]), ("network_down", "no_usable_connection"))
        self.recover_link("sim-b")
        self.assertEqual(self.states()["nav-a"], "active")

    def test_disconnected_interface_marks_dependent_session_down(self):
        self.a.register("nav-a", connection_id="sim-b")
        self.beat(self.a)
        self.net.set_state("sim-b", "disconnected")
        self.eng.refresh()
        self.assertEqual(self.states()["nav-a"], "network_down")


class ConcurrentStabilityTest(SessionApiBase):
    """Dos dueños latiendo en paralelo sobre HTTP real (loopback): sin errores ni contaminación cruzada."""

    def test_parallel_heartbeats(self):
        # Reloj que avanza 1 s en cada lectura: evita el límite de frecuencia sin esperar de verdad.
        counter = itertools.count(start=2000, step=1)
        lock = threading.Lock()

        def tick():
            with lock:
                return float(next(counter))
        self.eng.sessions.clock = tick
        sa = self.a.register("nav-a", connection_id="sim-a", heartbeat_timeout_s=300)["id"]
        sb = self.b.register("nav-b", connection_id="sim-b", heartbeat_timeout_s=300)["id"]
        errors, N = [], 100

        def run(client, ok):
            for _ in range(N):
                try:
                    client.heartbeat(ok, None if ok else "navigation_timeout")
                except Exception as e:   # noqa: BLE001 - se cuentan todos
                    errors.append(repr(e))
        ts = [threading.Thread(target=run, args=(self.a, True)), threading.Thread(target=run, args=(self.b, False))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errors, [])
        A, B = self.eng.sessions.get(sa, "", admin=True), self.eng.sessions.get(sb, "", admin=True)
        self.assertEqual((A.heartbeats, A.reported, A.error_code), (N, "ok", None))
        self.assertEqual((B.heartbeats, B.reported, B.error_code), (N, "error", "navigation_timeout"))


class RegistryUnitTest(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine()
        self.clock = FakeClock()
        self.reg = SessionRegistry(self.eng.audit, self.eng._session_network_state,
                                   lambda c: self.eng.connection(c) is not None, clock=self.clock)

    def test_pending_then_stale_without_heartbeat(self):
        s = self.reg.register("t1", "forms-a", {"label": "nav-a", "authorized_by_user": True, "heartbeat_timeout_s": 10})
        self.assertEqual(s.state, "pending")
        self.clock.advance(11)
        self.reg.evaluate()
        self.assertEqual((s.state, s.reason), ("stale", "no_heartbeat"))

    def test_restart_forgets_sessions(self):
        s = self.reg.register("t1", "forms-a", {"label": "nav-a", "authorized_by_user": True})
        fresh = SessionRegistry(self.eng.audit, self.eng._session_network_state, lambda c: True, clock=self.clock)
        with self.assertRaises(SessionError) as cm:
            fresh.heartbeat(s.id, "t1", {"status": "ok"})
        self.assertEqual(cm.exception.code, "unknown_session")

    def test_limits_validated(self):
        with self.assertRaises(ValueError):
            SessionRegistry(self.eng.audit, lambda c: "ok", lambda c: True, max_sessions=9)
        with self.assertRaises(SessionError):
            self.reg.register("t1", "x", {"label": "nav-a", "authorized_by_user": True, "heartbeat_timeout_s": 1})


if __name__ == "__main__":
    unittest.main()
