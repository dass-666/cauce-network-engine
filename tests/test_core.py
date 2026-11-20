import json
import os
import tempfile
import threading
import unittest

from helpers import sim_engine
from cauce.audit import AuditLog, verify_file
from cauce.config import ConfigError, load
from cauce.health import HealthConfig, HealthMonitor
from cauce.models import Connection, Health, Kind, LinkState, ProbeResult, TaskState
from cauce.policy import PolicyError
from cauce.routing import RouteError
from cauce.secrets_store import read_secret, write_secret
from cauce.simulator import SimLink, default_service


class FakeNet:
    def __init__(self):
        self.ok = True

    def probe(self, conn, target, timeout):
        return ProbeResult(conn.id, target, self.ok, 10.0 if self.ok else None, None if self.ok else "x", conn.primary_ip)


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.conn = Connection("c1", "c1", "", Kind.ETHERNET, LinkState.UP, ipv4=["10.0.0.2"], gateway="10.0.0.1")
        self.net = FakeNet()
        self.audit = AuditLog()
        self.mon = HealthMonitor(self.net, self.audit, HealthConfig(targets=["t:1"], interval_s=5, max_interval_s=40),
                                 lambda: [self.conn], clock=lambda: self.now[0])

    def test_hysteresis_and_backoff(self):
        self.assertEqual(self.mon.check(self.conn), Health.HEALTHY)
        self.net.ok = False
        self.assertEqual(self.mon.check(self.conn), Health.DEGRADED)  # 1 fallo: no se declara caída
        self.assertEqual(self.mon.check(self.conn), Health.DEGRADED)
        self.assertEqual(self.mon.check(self.conn), Health.DOWN)      # 3 fallos seguidos
        delays = []
        for _ in range(6):
            self.mon.check(self.conn)
            delays.append(self.mon._state["c1"].next_due - self.now[0])
        self.assertEqual(delays[-1], 40, "el retroceso exponencial está acotado")
        self.assertTrue(all(b >= a for a, b in zip(delays, delays[1:])))
        self.net.ok = True
        self.assertEqual(self.mon.check(self.conn), Health.DOWN, "un éxito no basta para recuperar")
        self.assertEqual(self.mon.check(self.conn), Health.HEALTHY)

    def test_no_targets_means_unknown_not_invented(self):
        self.mon.cfg.targets = []
        self.assertEqual(self.mon.check(self.conn), Health.UNKNOWN)

    def test_link_down_counts_as_failure(self):
        self.conn.link = LinkState.DOWN
        for _ in range(3):
            st = self.mon.check(self.conn)
        self.assertEqual(st, Health.DOWN)


class RoutingTest(unittest.TestCase):
    def test_explicit_unavailable_is_rejected(self):
        eng, _ = sim_engine()
        with self.assertRaises(RouteError) as cm:
            eng.routes.acquire("o", "p", "sim-c")
        self.assertEqual(cm.exception.code, "connection_unavailable")

    def test_owner_protection(self):
        eng, _ = sim_engine()
        l = eng.routes.acquire("app-1", "x", "sim-a")
        with self.assertRaises(RouteError) as cm:
            eng.routes.release(l.id, "app-2")
        self.assertEqual(cm.exception.code, "not_owner")
        self.assertFalse(l.released)

    def test_failover_of_one_lease_does_not_move_others(self):
        eng, net = sim_engine()
        on_a = eng.routes.acquire("t1", "uno", "sim-a")
        on_b = eng.routes.acquire("t2", "dos", "sim-b")
        pinned = eng.routes.acquire("t3", "fijo", "sim-a", allow_failover=False)
        net.set_state("sim-a", "down")
        for _ in range(3):
            eng.health.check(eng.connection("sim-a"))
        self.assertEqual(on_a.connection_id, "sim-b")
        self.assertEqual(on_b.connection_id, "sim-b")
        self.assertEqual(on_b.history[-1]["reason"], "acquire", "el lease de otra tarea no se tocó")
        self.assertEqual(pinned.connection_id, "sim-a", "sin permiso de conmutación no se mueve")

    def test_concurrent_acquire_release(self):
        eng, _ = sim_engine()
        errors = []

        def worker(i):
            try:
                for _ in range(50):
                    l = eng.routes.acquire(f"w{i}", "c", "sim-a" if i % 2 else "sim-b")
                    assert l.connection_id == ("sim-a" if i % 2 else "sim-b")
                    eng.routes.release(l.id, f"w{i}")
            except Exception as e:
                errors.append(e)
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        self.assertEqual(errors, [])
        self.assertEqual(eng.routes.leases(), [])


class ExecutorTest(unittest.TestCase):
    def test_non_idempotent_lost_response_is_not_repeated(self):
        eng, net = sim_engine()
        net.links["sim-a"].drop_responses = True
        t = eng.start_task({"name": "pago", "owner": "o", "connection_id": "sim-a",
                            "steps": [{"method": "POST", "url": "http://lab.sim/write"}]}, run_async=False)
        self.assertEqual(t.state, TaskState.NEEDS_REVIEW)
        self.assertEqual(net.applied.get("POST http://lab.sim/write"), 1, "se aplicó exactamente una vez")
        self.assertEqual(len([x for x in net.traffic if x.method == "POST"]), 1)

    def test_idempotency_key_allows_safe_retry(self):
        eng, net = sim_engine()
        net.links["sim-a"].drop_responses = True
        t = eng.start_task({"name": "w", "owner": "o", "connection_id": "sim-a", "max_attempts": 2,
                            "steps": [{"method": "POST", "url": "http://lab.sim/write", "idempotency_key": "k1"}]}, run_async=False)
        self.assertEqual(t.state, TaskState.FAILED)  # sigue sin respuesta, pero
        self.assertEqual(net.applied.get("k1"), 1, "el servidor deduplicó por clave")

    def test_403_stops_without_other_connection(self):
        eng, net = sim_engine(service=lambda l, r: (403, {}, b""))
        t = eng.start_task({"name": "x", "owner": "o", "steps": [{"url": "http://lab.sim/a"}]}, run_async=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertEqual({x.connection_id for x in net.traffic}, {"sim-a"})

    def test_503_with_retry_after_is_throttle(self):
        eng, net = sim_engine(service=lambda l, r: (503, {"retry-after": "5"}, b""))
        t = eng.start_task({"name": "x", "owner": "o", "steps": [{"url": "http://lab.sim/a"}] * 3}, run_async=False)
        self.assertEqual(t.state, TaskState.THROTTLED)
        self.assertEqual(len(net.traffic), 1)

    def test_transient_error_does_not_failover(self):
        calls = {"n": 0}
        eng, net = sim_engine()
        orig = net.request

        def flaky(conn, req):
            calls["n"] += 1
            if calls["n"] == 1:
                from cauce.net import ConnectFailed
                raise ConnectFailed("reset puntual")
            return orig(conn, req)
        eng.executor.network = type("N", (), {"request": staticmethod(flaky), "probe": net.probe})()
        t = eng.start_task({"name": "x", "owner": "o", "connection_id": "sim-a", "steps": [{"url": "http://lab.sim/a"}]}, run_async=False)
        self.assertEqual(t.state, TaskState.COMPLETED)
        self.assertTrue(all(h.get("kind") != "failover" for h in t.lease.history), "un error aislado no conmuta")

    def test_route_mismatch_stops_task(self):
        eng, net = sim_engine()
        orig = net.request

        def wrong(conn, req):
            r = orig(conn, req)
            r.local_ip = "10.99.99.99"
            return r
        eng.executor.network = type("N", (), {"request": staticmethod(wrong), "probe": net.probe})()
        t = eng.start_task({"name": "x", "owner": "o", "steps": [{"url": "http://lab.sim/a"}] * 2}, run_async=False)
        self.assertEqual(t.state, TaskState.FAILED)
        self.assertIn("route_mismatch", t.reason)
        self.assertEqual(t.current_step, 0)

    def test_disallowed_target_rejected(self):
        eng, _ = sim_engine()
        from cauce.engine import EngineError
        with self.assertRaises(EngineError) as cm:
            eng.start_task({"name": "x", "owner": "o", "steps": [{"url": "https://example.com/"}]})
        self.assertEqual(cm.exception.code, "target_not_allowed")

    def test_stop_task_async(self):
        eng, _ = sim_engine()
        t = eng.start_task({"name": "larga", "owner": "o", "step_delay_s": 0.2, "steps": [{"url": "http://lab.sim/a"}] * 100})
        eng.executor.stop(t.id, "prueba")
        eng.executor.join_all(5)
        self.assertEqual(t.state, TaskState.STOPPED)
        self.assertTrue(t.lease.released)

    def test_vanished_adapter_fails_over(self):
        eng, net = sim_engine()
        calls = {"n": 0}
        orig = net.request

        def req(conn, r):
            calls["n"] += 1
            if calls["n"] == 1:
                net.set_state("sim-a", "disconnected")
                eng.refresh()  # el inventario ya no tiene IP para sim-a
            return orig(conn, r)
        eng.executor.network = type("N", (), {"request": staticmethod(req), "probe": net.probe})()
        t = eng.start_task({"name": "x", "owner": "o", "connection_id": "sim-a", "steps": [{"url": "http://lab.sim/a"}] * 3}, run_async=False)
        self.assertEqual(t.state, TaskState.COMPLETED, t.reason)
        self.assertEqual(t.lease.connection_id, "sim-b")

    def test_no_route_at_all(self):
        eng, net = sim_engine()
        for cid in ("sim-a", "sim-b"):
            net.set_state(cid, "down")
            for _ in range(3):
                eng.health.check(eng.connection(cid))
        from cauce.engine import EngineError
        with self.assertRaises(EngineError) as cm:
            eng.start_task({"name": "x", "owner": "o", "steps": [{"url": "http://lab.sim/a"}]})
        self.assertEqual(cm.exception.code, "no_route")


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine()
        self.now = [10_000.0]
        self.eng.rotation.clock = lambda: self.now[0]

    def test_limits_validated(self):
        with self.assertRaises(PolicyError):
            self.eng.rotation.create("p", ["sim-a", "sim-b"], interval_s=1)
        with self.assertRaises(PolicyError):
            self.eng.rotation.create("p", ["sim-a", "sim-b"], max_changes_per_hour=1000)
        with self.assertRaises(PolicyError):
            self.eng.rotation.create("p", ["sim-a"])

    def test_rotation_interval_and_hourly_cap(self):
        p = self.eng.rotation.create("p", ["sim-a", "sim-b"], interval_s=10, max_changes_per_hour=3)
        l = self.eng.routes.acquire("o", "lab", "sim-a")
        self.eng.rotation.attach("p", l)
        self.assertEqual(self.eng.rotation.tick(), [], "pausada por defecto")
        self.eng.rotation.set_state("p", "active")
        seq = []
        for _ in range(10):
            self.eng.rotation.tick()
            seq.append(l.connection_id)
            self.now[0] += 5
        self.assertEqual(len(p.changes), 3, "tope por hora respetado")
        self.assertEqual(p.state, "paused")
        self.assertEqual(seq[0], "sim-b")
        self.assertEqual(seq[1], "sim-b", "no cambia antes del intervalo mínimo")

    def test_rotation_never_uses_down_connection(self):
        p = self.eng.rotation.create("p", ["sim-a", "sim-b", "sim-c"], interval_s=10)
        l = self.eng.routes.acquire("o", "lab", "sim-a")
        self.eng.rotation.attach("p", l)
        self.eng.rotation.set_state("p", "active")
        seen = set()
        for _ in range(6):
            self.eng.rotation.tick()
            seen.add(l.connection_id)
            self.now[0] += 11
        self.assertNotIn("sim-c", seen)

    def test_stopped_is_final(self):
        self.eng.rotation.create("p", ["sim-a", "sim-b"], interval_s=10)
        self.eng.rotation.set_state("p", "stopped")
        with self.assertRaises(PolicyError):
            self.eng.rotation.set_state("p", "active")


class AuditConfigSecretsTest(unittest.TestCase):
    def test_tamper_detected_and_redaction(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.jsonl")
        a = AuditLog(p)
        a.record("x", "uno", password="hunter2", nested={"api_key": "k"})
        a.record("x", "dos")
        a.record("x", "tres")
        raw = open(p, encoding="utf-8").read()
        self.assertNotIn("hunter2", raw)
        self.assertEqual(verify_file(p), (True, None))
        lines = raw.splitlines()
        ev = json.loads(lines[1]); ev["message"] = "editado"; lines[1] = json.dumps(ev)
        open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        self.assertEqual(verify_file(p), (False, 2))
        # al reabrir, la cadena continúa
        a2 = AuditLog(os.path.join(d, "b.jsonl"))
        a2.record("x", "1")
        a3 = AuditLog(os.path.join(d, "b.jsonl"))
        a3.record("x", "2")
        self.assertEqual(verify_file(os.path.join(d, "b.jsonl")), (True, None))

    def test_config_rules(self):
        with self.assertRaises(ConfigError):
            load(overrides={"api": {"host": "0.0.0.0"}})
        with self.assertRaises(ConfigError):
            load(overrides={"wifi": {"password": "x"}})
        with self.assertRaises(ConfigError):
            load(overrides={"lab": {"allowed_hosts": []}})
        self.assertEqual(load(overrides={"data_dir": tempfile.mkdtemp()})["api"]["host"], "127.0.0.1")

    def test_secret_file_permissions(self):
        p = os.path.join(tempfile.mkdtemp(), "s")
        write_secret(p, "abc")
        self.assertEqual(read_secret(p), "abc")
        if os.name == "posix":
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
