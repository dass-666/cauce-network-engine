"""Pruebas con sockets REALES en 127.0.0.0/8 (no simulados).

Verifican el mecanismo de enlace a IP de origen y la API HTTP real. En Linux
127.0.0.2 es una IP de loopback distinta: el servidor ve el origen elegido.
Esto prueba el MECANISMO de selección por IP de origen, no la independencia
de dos salidas a internet (eso requiere hardware, ver docs/HARDWARE.md).
"""
import http.client
import json
import os
import sys
import tempfile
import unittest

from helpers import sim_engine
from cauce.api import CauceAPI
from cauce.audit import AuditLog
from cauce.client import CauceAPIError, CauceClient
from cauce.config import load
from cauce.engine import Engine
from cauce.labserver import LabServer
from cauce.models import Connection, Kind, LinkState, TaskState
from cauce.net import ConnectFailed, LabRequest, RealNetwork

MULTI_LOOPBACK = sys.platform.startswith("linux")


def conn(cid, ip):
    return Connection(cid, cid, "loopback de prueba", Kind.ETHERNET, LinkState.UP, ipv4=[ip], gateway=f"gw-{cid}")


class StaticDiscovery:
    name = "static"

    def __init__(self, conns):
        self.conns = conns

    def discover(self):
        return [Connection(**{**c.__dict__, "ipv4": list(c.ipv4)}) for c in self.conns]


class RealNetworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = LabServer(port=0).start()

    @classmethod
    def tearDownClass(cls):
        cls.lab.stop()

    def url(self, path):
        return f"http://127.0.0.1:{self.lab.port}{path}"

    def test_source_binding_is_observed_by_server(self):
        net = RealNetwork()
        r = net.request(conn("a", "127.0.0.1"), LabRequest("GET", self.url("/ok")))
        self.assertEqual((r.status, r.local_ip, r.observed_ip), (200, "127.0.0.1", "127.0.0.1"))
        if MULTI_LOOPBACK:
            r2 = net.request(conn("b", "127.0.0.2"), LabRequest("GET", self.url("/ok")))
            self.assertEqual((r2.local_ip, r2.observed_ip), ("127.0.0.2", "127.0.0.2"))

    def test_probe_real(self):
        net = RealNetwork()
        p = net.probe(conn("a", "127.0.0.1"), f"127.0.0.1:{self.lab.port}", 2)
        self.assertTrue(p.ok, p.error)
        self.assertEqual(p.local_ip_used, "127.0.0.1")
        bad = net.probe(conn("x", "10.255.255.254"), f"127.0.0.1:{self.lab.port}", 1)
        self.assertFalse(bad.ok)

    def test_ip_not_on_host_fails_before_sending(self):
        with self.assertRaises(ConnectFailed):
            RealNetwork().request(conn("x", "10.255.255.254"), LabRequest("GET", self.url("/ok"), timeout=2))

    @unittest.skipUnless(MULTI_LOOPBACK, "requiere 127.0.0.2 (Linux)")
    def test_engine_real_failover_and_throttle(self):
        d = tempfile.mkdtemp()
        a, b = conn("a", "127.0.0.1"), conn("b", "127.0.0.2")
        disc = StaticDiscovery([a, b])
        cfg = load(overrides={"data_dir": d, "health": {"targets": [f"127.0.0.1:{self.lab.port}"]},
                              "lab": {"allowed_hosts": ["127.0.0.1"]}})
        eng = Engine(cfg, disc, RealNetwork(), audit=AuditLog(os.path.join(d, "a.jsonl")))
        eng.refresh()
        eng.health.check_all(force=True)
        t = eng.start_task({"name": "real", "owner": "o", "connection_id": "b",
                            "steps": [{"url": self.url("/ok")}] * 3}, run_async=False)
        self.assertEqual(t.state, TaskState.COMPLETED)
        self.assertTrue(all(r["observed_ip"] == "127.0.0.2" and r["route_verified"] for r in t.results))
        # "b" deja de existir en el equipo (IP retirada): el bind falla de verdad -> conmutación a "a"
        disc.conns[1].ipv4 = ["10.255.255.254"]
        eng.refresh()
        t2 = eng.start_task({"name": "real2", "owner": "o", "steps": [{"url": self.url("/ok")}] * 2}, run_async=False)
        self.assertEqual(t2.state, TaskState.COMPLETED, t2.reason)
        n_before = self.lab.requests_total
        t3 = eng.start_task({"name": "lim", "owner": "o", "connection_id": "a",
                             "steps": [{"url": self.url("/limit")}, {"url": self.url("/ok")}]}, run_async=False)
        self.assertEqual(t3.state, TaskState.THROTTLED)
        self.assertEqual(self.lab.requests_total - n_before, 1, "una sola petición; no se reintentó por otra conexión")


    @unittest.skipUnless(MULTI_LOOPBACK, "requiere 127.0.0.2 (Linux)")
    def test_egress_check_marks_confirmed_independent(self):
        d = tempfile.mkdtemp()
        disc = StaticDiscovery([conn("a", "127.0.0.1"), conn("b", "127.0.0.2")])
        cfg = load(overrides={"data_dir": d, "egress_check_url": self.url("/ip"),
                              "health": {"targets": [f"127.0.0.1:{self.lab.port}"]}})
        eng = Engine(cfg, disc, RealNetwork(), audit=AuditLog(os.path.join(d, "a.jsonl")))
        eng.refresh()
        self.assertEqual(eng.connection("b").public_ip, "127.0.0.2")
        self.assertEqual(eng.connection("b").independence.value, "confirmed_independent")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine()
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.enable_sim_control(self.net)
        self.api.start()
        self.c = CauceClient(self.api.token, port=self.api.port)

    def tearDown(self):
        self.api.stop()

    def raw(self, method, path, headers=None, body=None):
        h = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=5)
        h.request(method, path, body=body, headers=headers or {})
        r = h.getresponse()
        return r.status, r.read()

    def test_only_loopback(self):
        with self.assertRaises(ValueError):
            CauceAPI(self.eng, "0.0.0.0", 0)
        self.assertEqual(self.api.httpd.server_address[0], "127.0.0.1")

    def test_security_headers(self):
        self.assertEqual(self.raw("GET", "/api/v1/ping")[0], 200)
        self.assertEqual(self.raw("GET", "/api/v1/connections")[0], 401)
        auth = {"Authorization": f"Bearer {self.api.token}"}
        self.assertEqual(self.raw("GET", "/api/v1/connections", {**auth, "Host": "evil.example:80"})[0], 421)
        self.assertEqual(self.raw("POST", "/api/v1/stop-all", {**auth, "Origin": "http://evil.example"}, b"{}")[0], 403)
        self.assertEqual(self.raw("GET", "/")[0], 401, "el panel exige el token en la URL")
        st, body = self.raw("GET", f"/?t={self.api.token}")
        self.assertEqual(st, 200)
        self.assertIn(b"Cauce Network Engine", body)
        self.assertEqual(self.raw("POST", "/api/v1/tasks", {**auth, "Content-Type": "application/json"}, b"{nope")[0], 400)

    def test_full_flow_with_client(self):
        self.assertEqual(self.c.ping()["api"], "v1")
        conns = {x["id"]: x for x in self.c.connections()}
        self.assertEqual(set(conns), {"sim-a", "sim-b", "sim-c"})
        self.assertTrue(conns["sim-a"]["available"])
        self.assertFalse(conns["sim-c"]["available"])
        self.c.select("sim-b")
        route = self.c.request_route(owner="app-x", purpose="sync")
        self.assertEqual(route["connection_id"], "sim-b")
        self.assertEqual(route["bind"]["source_ip"], "10.20.2.30")
        with self.assertRaises(CauceAPIError) as cm:
            self.c.release_route(route["id"], owner="otra-app")
        self.assertEqual(cm.exception.status, 403)
        self.c.release_route(route["id"], owner="app-x")
        with self.assertRaises(CauceAPIError) as cm:
            self.c.request_route(owner="app-x", purpose="sync", connection_id="sim-c")
        self.assertEqual(cm.exception.code, "connection_unavailable")
        t = self.c.start_task({"name": "api", "owner": "app-x", "connection_id": "sim-a",
                               "steps": [{"url": "http://lab.sim/ok"}] * 3})
        self.eng.executor.join_all(5)
        self.assertEqual(self.c.task(t["id"])["state"], "completed")
        with self.assertRaises(CauceAPIError) as cm:
            self.c.start_task({"name": "fuera", "owner": "app-x", "steps": [{"url": "https://example.com/"}]})
        self.assertEqual(cm.exception.code, "target_not_allowed")
        p = self.c.create_rotation("rr", ["sim-a", "sim-b"], 10, 5)
        self.assertEqual(p["state"], "paused")
        self.assertEqual(self.c.set_rotation("rr", "activate")["state"], "active")
        self.assertEqual(self.c.stop_all("fin")["tasks_stopped"], 0)
        self.assertEqual(self.c.rotation()[0]["state"], "stopped")
        ev = self.c.events(kind="route")
        self.assertTrue(any(e["kind"] == "route.selected" for e in ev["events"]))
        self.assertFalse(self.c.diagnostics()["global_routes_modified"])

    def test_sim_control_triggers_failover_via_api(self):
        route = self.c.request_route(owner="app", purpose="x", connection_id="sim-a")
        for _ in range(3):
            self.raw("POST", "/api/v1/sim/connections/sim-a/state",
                     {"Authorization": f"Bearer {self.api.token}", "Content-Type": "application/json"},
                     json.dumps({"state": "down"}).encode())
        self.assertEqual(self.c.route(route["id"])["connection_id"], "sim-b")


if __name__ == "__main__":
    unittest.main()
