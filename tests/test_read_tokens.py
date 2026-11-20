"""Credenciales ``read``, vista reducida de conectividad y límites de seguridad.

Todo corre contra el simulador y sockets de loopback: NO es una prueba de hardware ni de Windows físico.
Las pruebas negativas comprueban el EFECTO (nada cambió en el motor), no solo el código HTTP.
"""
import contextlib
import http.client
import io
import json
import os
import socket
import tempfile
import unittest
from collections import deque

from helpers import sim_engine
from cauce.__main__ import main
from cauce.api import CauceAPI
from cauce.client import CauceAPIError, CauceClient, CauceReadClient
from cauce.secrets_store import write_secret
from cauce.tokens import TokenError, TokenStore

# Datos del simulador que NUNCA deben aparecer en una respuesta de lectura
SIM_SECRETS = ["10.10.1.20", "10.10.1.1", "10.20.2.30", "10.20.2.1", "198.51.100.10", "203.0.113.20",
               "192.0.2.30", "public_ip", "ipv4", "gateway", "primary_ip", "\"mac\"", "egress_group", "owner", "purpose"]


class Base(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine()
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.enable_sim_control(self.net)
        self.api.start()
        self.admin = CauceClient(self.api.token, port=self.api.port)
        rec = self.admin.create_token("monitor", "read")
        self.read_token, self.read_id = rec["token"], rec["id"]
        self.read = CauceReadClient(self.read_token, port=self.api.port)

    def tearDown(self):
        self.api.stop()

    def raw(self, method, path, token=None, body=None, headers=None):
        h = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=5)
        hd = dict(headers or {})
        if token:
            hd["Authorization"] = f"Bearer {token}"
        if body is not None:
            hd["Content-Type"] = "application/json"
            body = json.dumps(body)
        h.request(method, path, body=body, headers=hd)
        r = h.getresponse()
        data = r.read().decode("utf-8")
        h.close()
        return r.status, data

    def code(self, data):
        return json.loads(data).get("error", {}).get("code")


class ReadTokenAuthTest(Base):
    def test_valid_read_token(self):
        st, data = self.raw("GET", "/api/v1/status/connectivity", self.read_token)
        self.assertEqual(st, 200)
        v = json.loads(data)
        self.assertEqual(v["schema"], "cauce.connectivity/1")
        self.assertEqual(v["state"], "usable")
        self.assertIs(v["usable"], True)
        self.assertEqual({c["id"] for c in v["connections"]}, {"sim-a", "sim-b", "sim-c"})

    def test_invalid_tokens_are_401(self):
        secret = self.read_token.split(".")[2]
        for bad in (None, "", "basura", "cr1.deadbeef." + secret, f"cr1.{self.read_id}." + "x" * 43,
                    f"cr1.{self.read_id}", self.read_token + "x"):
            st, data = self.raw("GET", "/api/v1/status/connectivity", bad)
            self.assertEqual((st, self.code(data)), (401, "unauthorized"), bad)

    def test_revoked_token_stops_working_immediately(self):
        self.assertEqual(self.raw("GET", "/api/v1/status/connectivity", self.read_token)[0], 200)
        self.admin.revoke_token(self.read_id)
        st, data = self.raw("GET", "/api/v1/status/connectivity", self.read_token)
        self.assertEqual((st, self.code(data)), (401, "token_revoked"))
        # y el nombre queda libre para una credencial nueva
        self.assertEqual(self.admin.create_token("monitor")["name"], "monitor")

    def test_admin_token_still_reads_everything(self):
        self.assertEqual(self.raw("GET", "/api/v1/status/connectivity", self.api.token)[0], 200)
        self.assertEqual(len(self.admin.connections()), 3)

    def test_token_management_rules(self):
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.create_token("otro", "admin")
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "invalid_scope"))
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.create_token("monitor")
        self.assertEqual(cm.exception.code, "name_in_use")
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.create_token("Nombre Malo!")
        self.assertEqual(cm.exception.code, "invalid_name")
        with self.assertRaises(CauceAPIError) as cm:
            self.admin.revoke_token("00000000")
        self.assertEqual(cm.exception.status, 404)
        listing = json.dumps(self.admin.tokens())
        self.assertNotIn(self.read_token.split(".")[2], listing)
        self.assertNotIn("hash", listing)


class ReadTokenDenialTest(Base):
    """Un token read no puede hacer NADA administrativo, y se comprueba que no ocurrió."""

    def test_admin_operations_denied_without_side_effects(self):
        eng = self.eng
        calls = {"refresh": 0, "check": 0}
        orig_refresh, orig_check = eng.refresh, eng.health.check
        eng.refresh = lambda *a, **k: (calls.__setitem__("refresh", calls["refresh"] + 1), orig_refresh(*a, **k))[1]
        eng.health.check = lambda *a, **k: (calls.__setitem__("check", calls["check"] + 1), orig_check(*a, **k))[1]
        probes_before = eng.health.snapshot()["sim-a"]["probes"]
        tokens_before = len(self.admin.tokens())
        tk = self.read_token
        attempts = [
            ("GET", "/api/v1/connections", None), ("GET", "/api/v1/connections?refresh=1", None),
            ("GET", "/api/v1/connections/sim-a", None), ("POST", "/api/v1/connections/sim-a/check", {}),
            ("GET", "/api/v1/diagnostics", None), ("GET", "/api/v1/events", None),
            ("GET", "/api/v1/routes", None), ("GET", "/api/v1/tasks", None), ("GET", "/api/v1/rotation", None),
            ("POST", "/api/v1/routes", {"owner": "intruso", "purpose": "x"}),
            ("POST", "/api/v1/selection", {"connection_id": "sim-b"}),
            ("POST", "/api/v1/tasks", {"kind": "http", "url": "http://127.0.0.1/"}),
            ("POST", "/api/v1/rotation", {"name": "r", "pool": ["sim-a", "sim-b"]}),
            ("POST", "/api/v1/rotation/r/activate", {}),
            ("POST", "/api/v1/stop-all", {}), ("POST", "/api/v1/shutdown", {}),
            ("POST", "/api/v1/sim/connections/sim-a/state", {"state": "down"}),
            ("GET", "/api/v1/tokens", None), ("POST", "/api/v1/tokens", {"name": "ataque"}),
            ("DELETE", f"/api/v1/tokens/{self.read_id}", None),
            ("DELETE", "/api/v1/routes/ls_abc?owner=x", None),
        ]
        for method, path, body in attempts:
            st, data = self.raw(method, path, tk, body)
            self.assertEqual((st, self.code(data)), (403, "insufficient_scope"), f"{method} {path}")
        # efectos: nada cambió
        self.assertEqual(calls, {"refresh": 0, "check": 0}, "un token read provocó detección o sondeo")
        self.assertEqual(eng.health.snapshot()["sim-a"]["probes"], probes_before)
        self.assertFalse(self.api.shutdown_requested.is_set())
        self.assertIsNone(eng.routes.operator_choice)
        self.assertEqual(eng.routes.leases(), [])
        self.assertEqual(eng.executor.all(), [])
        self.assertEqual(eng.rotation.all(), [])
        self.assertEqual(self.net.links["sim-a"].state, "up")
        self.assertEqual(len(self.admin.tokens()), tokens_before)
        self.assertEqual(self.raw("GET", "/api/v1/status/connectivity", tk)[0], 200, "el token sigue vigente")

    def test_refresh_param_rejected_on_read_views_and_never_executes(self):
        calls = []
        self.eng.refresh = lambda *a, **k: calls.append("refresh")
        self.eng.health.check = lambda *a, **k: calls.append("check")
        self.eng.health.check_all = lambda *a, **k: calls.append("check_all")
        for path in ("/api/v1/status/connectivity?refresh=1", "/api/v1/status/events?refresh=1", "/api/v1/status/events?check=1"):
            st, data = self.raw("GET", path, self.read_token)
            self.assertEqual((st, self.code(data)), (400, "unsupported_parameter"), path)
        self.assertEqual(calls, [])

    def test_read_views_never_trigger_detection_or_probes(self):
        discover_calls = []
        orig = self.net.discover
        self.net.discover = lambda *a, **k: (discover_calls.append(1), orig(*a, **k))[1]
        calls = []
        self.eng.refresh = lambda *a, **k: calls.append("refresh")
        self.eng.health.check = lambda *a, **k: calls.append("check")
        self.eng.health.check_all = lambda *a, **k: calls.append("check_all")
        probes = self.eng.health.snapshot()["sim-a"]["probes"]
        for _ in range(5):
            self.read.connectivity()
            self.read.events(0)
        self.assertEqual((calls, discover_calls), ([], []))
        self.assertEqual(self.eng.health.snapshot()["sim-a"]["probes"], probes)


class ReducedViewTest(Base):
    def test_no_machine_data_in_any_read_response(self):
        self.admin.select("sim-b")                      # genera datos de rutas/operador que no deben salir
        self.admin.request_route(owner="otro-sistema", purpose="secreto-de-otro")
        self.net.set_state("sim-b", "disconnected")
        self.eng.refresh()                              # genera inventory.changed con IPs en el mensaje original
        raw_events = json.dumps(self.admin.events(0))
        self.assertIn("10.20.2.30", raw_events, "la prueba solo vale si el evento original SÍ contiene IPs")
        for path in ("/api/v1/status/connectivity", "/api/v1/status/events?since=0&limit=500"):
            st, data = self.raw("GET", path, self.read_token)
            self.assertEqual(st, 200)
            for s in SIM_SECRETS + [self.read_token, self.api.token, "otro-sistema", "SIM Fibra", "SIM LTE"]:
                self.assertNotIn(s, data, f"{path} expone {s!r}")

    def test_field_whitelist_is_exact(self):
        v = self.read.connectivity()
        self.assertEqual(set(v), {"schema", "api", "engine", "generated_at", "last_seq", "state", "usable",
                                  "reason", "counts", "connections"})
        for c in v["connections"]:
            self.assertEqual(set(c), {"id", "kind", "link", "available", "measured", "health",
                                      "availability_pct", "avg_latency_ms"})
        for ev in self.read.events(0)["events"]:
            self.assertLessEqual(set(ev), {"seq", "ts", "kind", "connection_id", "old", "new"})

    def test_state_unknown_without_measurements(self):
        self.eng.health._state.clear()
        v = self.read.connectivity()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unknown", None, "insufficient_measurements"))
        self.assertEqual(v["counts"]["measured"], 0)
        self.assertTrue(all(c["measured"] is False for c in v["connections"]))

    def test_state_unavailable_when_measured_and_nothing_available(self):
        for cid in ("sim-a", "sim-b"):
            self.net.set_state(cid, "disconnected")
        self.eng.refresh()
        v = self.read.connectivity()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unavailable", False, "no_active_links"))

    def test_state_unavailable_when_probes_fail(self):
        for cid in ("sim-a", "sim-b"):
            self.net.set_state(cid, "down")        # enlace arriba, pero los sondeos fallan
        for _ in range(4):
            for cid in ("sim-a", "sim-b"):
                self.eng.health.check(self.eng.connection(cid))
        v = self.read.connectivity()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unavailable", False, "all_measured_connections_down"))

    def test_state_degraded(self):
        self.eng.health._state["sim-a"].state = __import__("cauce.models", fromlist=["Health"]).Health.DEGRADED
        self.eng.health._state["sim-b"].state = __import__("cauce.models", fromlist=["Health"]).Health.DEGRADED
        v = self.read.connectivity()
        self.assertEqual((v["state"], v["usable"]), ("degraded", True))

    def test_engine_unreachable_is_not_no_internet(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        c = CauceReadClient(self.read_token, port=port, timeout=1.0)
        v = c.connectivity_or_unknown()
        self.assertEqual((v["state"], v["usable"]), ("engine_unreachable", None))
        self.assertNotEqual(v["state"], "unavailable")
        self.assertIsNone(v["usable"], "'sin información' no puede ser False")


class EventsGapTest(Base):
    def test_gap_detection_and_resync(self):
        a = self.eng.audit
        a._events = deque(a._events, maxlen=20)         # memoria pequeña para provocar pérdida
        for i in range(60):
            a.record("health.change", "x", connection_id="sim-a", old="healthy", new="degraded")
        v = self.read.events(since=0)
        self.assertTrue(v["gap"] and v["resync_required"])
        # tras releer el estado completo y avanzar a last_seq no hay hueco
        full = self.read.connectivity()
        v2 = self.read.events(since=full["last_seq"])
        self.assertFalse(v2["gap"] or v2["resync_required"])
        self.assertEqual(v2["events"], [])
        # dentro de la ventana conservada tampoco hay hueco
        oldest = a._events[0]["seq"]
        self.assertFalse(self.read.events(since=oldest - 1)["gap"])
        self.assertTrue(self.read.events(since=oldest - 2)["gap"])

    def test_paging_is_chronological_and_complete(self):
        a = self.eng.audit
        base = a.last_seq
        for i in range(12):
            a.record("health.change", "x", connection_id="sim-a", old="healthy", new="degraded")
        seen, since, pages = [], base, 0
        while True:
            v = self.read.events(since=since, limit=5)
            seen += [e["seq"] for e in v["events"]]
            since = v["next_since"]
            pages += 1
            if not v["has_more"]:
                break
        self.assertEqual(seen, list(range(base + 1, base + 13)))
        self.assertEqual(pages, 3)
        self.assertEqual(self.read.events(since=since)["events"], [])

    def test_restart_detected_by_epoch_and_since_beyond_last(self):
        v = self.read.events(since=10_000)
        self.assertTrue(v["restarted"] and v["resync_required"])
        self.assertEqual(v["next_since"], v["last_seq"])
        self.assertEqual(self.read.connectivity()["engine"]["epoch"], v["epoch"])

    def test_health_change_visible_to_consumer(self):
        since = self.read.connectivity()["last_seq"]
        self.net.set_state("sim-a", "down")
        for _ in range(4):
            self.eng.health.check(self.eng.connection("sim-a"))
        evs = self.read.events(since=since)["events"]
        self.assertTrue(any(e["kind"] == "health.change" and e["connection_id"] == "sim-a" and e["new"] == "down" for e in evs))


class AccessLogAndStoreTest(Base):
    def test_access_log_has_names_but_no_secrets(self):
        self.raw("GET", "/api/v1/status/events?since=5&limit=3", self.read_token)
        self.raw("GET", "/api/v1/connections", self.read_token)
        self.raw("GET", "/api/v1/status/connectivity", "cr1.deadbeef.zzzzzzzzzzzzzzzzzzzzzzzz")
        with open(self.api.access._path, encoding="utf-8") as fh:
            text = fh.read()
        rows = [json.loads(l) for l in text.splitlines()]
        mine = [r for r in rows if r["who"] == "monitor"]
        self.assertEqual([r["status"] for r in mine], [200, 403])
        self.assertEqual(mine[0]["path"], "/api/v1/status/events?limit,since", "solo claves de la consulta, sin valores")
        self.assertTrue(any(r["status"] == 401 and r["who"] is None for r in rows))
        for secret in (self.read_token, self.read_token.split(".")[2], self.api.token, "deadbeef.zzzz"):
            self.assertNotIn(secret, text)
        # la auditoría con hashes tampoco guarda secretos y las lecturas no la llenan
        audit_text = json.dumps(self.eng.audit.events(0))
        self.assertNotIn(self.read_token.split(".")[2], audit_text)
        self.assertTrue(any(e["kind"] == "token.created" for e in self.eng.audit.events(0)))
        seq = self.eng.audit.last_seq
        for _ in range(10):
            self.read.connectivity()
        self.assertEqual(self.eng.audit.last_seq, seq, "las lecturas no deben crear eventos (provocarían huecos)")

    def test_store_persists_hashes_only(self):
        path = self.api.tokens._path
        with open(path, "rb") as fh:
            blob = fh.read()
        self.assertNotIn(self.read_token.split(".")[2].encode(), blob)
        again = TokenStore(path)
        self.assertEqual(again.verify(self.read_token).name, "monitor")
        self.admin.revoke_token(self.read_id)
        with self.assertRaises(TokenError) as cm:
            TokenStore(path).verify(self.read_token)
        self.assertEqual(cm.exception.code, "token_revoked")

    def test_unreadable_store_grants_nothing(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tokens.dat")
        with open(p, "wb") as fh:
            fh.write(b"basura")
        with self.assertRaises(TokenError):
            TokenStore(p).verify(self.read_token)


class CliAndCompatTest(Base):
    def test_cli_create_to_file_list_revoke(self):
        d = self.eng.cfg["data_dir"]
        write_secret(os.path.join(d, "api.token"), self.api.token)
        with open(os.path.join(d, "api.json"), "w") as fh:
            json.dump({"port": self.api.port}, fh)
        cfgp = os.path.join(d, "cfg.json")
        with open(cfgp, "w") as fh:
            json.dump({"data_dir": d}, fh)
        out_file = os.path.join(d, "lector.token")

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["--config", cfgp, *argv])
            return rc, buf.getvalue()

        rc, out = run("token", "create", "--name", "form-connector", "--out", out_file)
        self.assertEqual(rc, 0)
        self.assertNotIn(b"cr1.", out.encode(), "el token no debe mostrarse en consola con --out")
        reader = CauceReadClient.from_token_file(out_file, port=self.api.port)
        self.assertEqual(reader.connectivity()["schema"], "cauce.connectivity/1")
        rc, out = run("token", "list")
        self.assertIn("form-connector", out)
        tid = [t["id"] for t in self.admin.tokens() if t["name"] == "form-connector"][0]
        self.assertEqual(run("token", "revoke", tid)[0], 0)
        with self.assertRaises(CauceAPIError) as cm:
            reader.connectivity()
        self.assertEqual(cm.exception.code, "token_revoked")

    def test_existing_endpoints_and_v010_shapes_unchanged_for_admin(self):
        c = self.admin
        self.assertEqual(c.ping()["api"], "v1")
        conn = c.connection("sim-a")
        self.assertIn("mac", conn)
        self.assertIn("public_ip", conn)           # el cliente admin sigue viendo lo mismo que en v0.1.0
        ev = c.events(0)
        self.assertIn("events", ev)
        self.assertIn("last_seq", ev)
        self.assertEqual(self.raw("GET", "/api/v1/ping")[0], 200)
        self.assertEqual(self.raw("GET", "/api/v1/connections")[0], 401)
        self.assertEqual(self.raw("GET", "/api/v1/no-existe", self.read_token)[0], 404)


if __name__ == "__main__":
    unittest.main()


class RevisionPr45Test(Base):
    """Cierre de la revisión de las PR #4 y #5: cobertura exhaustiva, almacenamiento y secretos."""

    READ_ROUTES = {("GET", "/api/v1/status/connectivity"), ("GET", "/api/v1/status/events")}

    def test_every_registered_route_is_denied_to_read_unless_whitelisted(self):
        """Recorre las rutas REALES del servidor (no una lista a mano): una ruta nueva sin declarar queda como admin."""
        import re
        allowed, denied = set(), 0
        for method, pat, _fn, need in self.api.routes:
            raw = pat.pattern[1:-1]
            def fill(m):
                body = m.group(1)
                return next(c for c in ("sim-a", "00000000", "ls_abc", "tk_abc", "activate", "r", "1")
                            if re.fullmatch(body, c))
            path = re.sub(r"\(\?P<\w+>((?:[^()]|\([^()]*\))*)\)", fill, raw)
            if not pat.match(path):
                path = None
            self.assertIsNotNone(path, f"no se pudo construir una ruta de ejemplo para {raw}")
            if need == "read":
                allowed.add((method, path))
                continue
            if path == "/api/v1/ping":   # público por diseño (sin token), no expone nada sensible
                continue
            st, data = self.raw(method, path, self.read_token, {} if method != "GET" else None)
            self.assertEqual((st, self.code(data)), (403, "insufficient_scope"), f"{method} {path}")
            denied += 1
        self.assertEqual(allowed, self.READ_ROUTES, "el conjunto de rutas accesibles a read cambió: revisar a propósito")
        self.assertGreaterEqual(denied, 20)
        self.assertEqual(len(self.admin.tokens()), 1, "un token read no debe crear ni revocar credenciales")

    def test_admin_token_never_in_read_responses_or_logs(self):
        texts = []
        for path in ("/api/v1/status/connectivity", "/api/v1/status/events?since=0", "/api/v1/ping"):
            texts.append(self.raw("GET", path, self.read_token)[1])
        texts.append(self.raw("GET", "/api/v1/connections", self.read_token)[1])   # 403
        texts.append(self.raw("GET", "/api/v1/status/connectivity", "cr1.deadbeef." + "a" * 30)[1])  # 401
        for t in texts:
            self.assertNotIn(self.api.token, t)
        for name in os.listdir(self.eng.cfg["data_dir"]):
            p = os.path.join(self.eng.cfg["data_dir"], name)
            if name in ("access.jsonl", "audit.jsonl"):
                with open(p, encoding="utf-8") as fh:
                    self.assertNotIn(self.api.token, fh.read(), name)

    def _store(self):
        d = tempfile.mkdtemp()
        return TokenStore(os.path.join(d, "tokens.dat"))

    def test_create_rolls_back_when_storage_fails(self):
        from unittest import mock
        st = self._store()
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("disco lleno")):
            with self.assertRaises(TokenError) as cm:
                st.create("sistema-a")
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "storage_error"))
        self.assertEqual(st.list(), [], "no debe quedar una credencial fantasma")
        st.create("sistema-a")   # el nombre sigue libre

    def test_revocation_holds_in_memory_and_can_be_retried_when_storage_fails(self):
        from unittest import mock
        st = self._store()
        rec, tok = st.create("sistema-a")
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("solo lectura")):
            with self.assertRaises(TokenError) as cm:
                st.revoke(rec["id"])
        self.assertEqual(cm.exception.code, "revocation_not_persisted")
        with self.assertRaises(TokenError) as cm:
            st.verify(tok)
        self.assertEqual(cm.exception.code, "token_revoked", "falla cerrado: sigue revocada en memoria")
        st.revoke(rec["id"])   # reintento con el almacenamiento sano: ahora sí se guarda
        with self.assertRaises(TokenError) as cm:
            TokenStore(st._path).verify(tok)
        self.assertEqual(cm.exception.code, "token_revoked", "tras reiniciar sigue revocada")

    def test_unreadable_store_is_set_aside_not_overwritten(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tokens.dat")
        with open(p, "wb") as fh:
            fh.write(b"basura")
        st = TokenStore(p)
        st.create("sistema-a")
        with open(p + ".unreadable", "rb") as fh:
            self.assertEqual(fh.read(), b"basura")

    def test_api_returns_503_on_storage_error_and_engine_keeps_serving(self):
        from unittest import mock
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("disco lleno")):
            with self.assertRaises(CauceAPIError) as cm:
                self.admin.create_token("otro")
        self.assertEqual(cm.exception.status, 503)
        self.assertEqual(self.raw("GET", "/api/v1/status/connectivity", self.read_token)[0], 200)
