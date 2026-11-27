"""0.2.0: recuperación confirmada sin esperar el retroceso acumulado, y estados viejos que no se muestran como sanos."""
import time
import unittest

from helpers import sim_engine
from cauce.audit import AuditLog
from cauce.health import HealthConfig, HealthMonitor
from cauce.models import Connection, Health, Kind, LinkState
from cauce.status import connectivity_view
from test_core import FakeNet


class RecoveryConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.conn = Connection("c1", "c1", "", Kind.ETHERNET, LinkState.UP, ipv4=["10.0.0.2"], gateway="10.0.0.1")
        self.net = FakeNet()
        self.mon = HealthMonitor(self.net, AuditLog(), HealthConfig(targets=["t:1"], interval_s=5, max_interval_s=60),
                                 lambda: [self.conn], clock=lambda: self.now[0])

    def _due(self):
        return self.mon._state["c1"].next_due - self.now[0]

    def test_first_ok_after_down_schedules_confirmation_at_normal_interval(self):
        self.net.ok = False
        for _ in range(8):
            self.mon.check(self.conn)
        self.assertEqual(self._due(), 60, "caída prolongada: retroceso al máximo")
        self.net.ok = True
        self.assertEqual(self.mon.check(self.conn), Health.DOWN, "la histéresis no cambia: un éxito no basta")
        self.assertEqual(self._due(), 5, "la confirmación se hace al intervalo normal, no tras otros 60 s")
        self.assertEqual(self.mon.check(self.conn), Health.HEALTHY)
        self.assertEqual(self.mon._state["c1"].backoff_level, 0)

    def test_failed_confirmation_returns_to_backoff(self):
        self.net.ok = False
        for _ in range(8):
            self.mon.check(self.conn)
        self.net.ok = True
        self.mon.check(self.conn)
        self.net.ok = False
        self.mon.check(self.conn)
        self.assertEqual(self._due(), 60, "si la confirmación falla, se vuelve a esperar lo acotado")

    def test_worst_case_recovery_is_one_long_wait_plus_one_interval(self):
        """Peor caso tras reconectar: max_interval (sondeo ya programado) + interval (confirmación)."""
        self.net.ok = False
        for _ in range(8):
            self.mon.check(self.conn)
        start = self.now[0]
        self.net.ok = True
        while self.mon.state_of("c1") != Health.HEALTHY:
            self.now[0] = self.mon._state["c1"].next_due
            self.mon.check(self.conn)
        self.assertLessEqual(self.now[0] - start, 60 + 5)


class StaleViewTest(unittest.TestCase):
    def test_fresh_view_reports_probe_age_and_is_usable(self):
        eng, _ = sim_engine()
        v = connectivity_view(eng, time.time())
        self.assertEqual(v["state"], "usable")
        for c in v["connections"]:
            if c["measured"]:
                self.assertFalse(c["stale"])
                self.assertLess(c["last_probe_age_s"], 30)

    def test_old_measurements_are_not_reported_as_healthy(self):
        eng, _ = sim_engine()
        later = time.time() + eng.health.stale_after_s() + 1     # el hilo de salud dejó de sondear
        v = connectivity_view(eng, later)
        self.assertTrue(all(c["stale"] for c in v["connections"] if c["measured"]))
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unknown", None, "stale_measurements"))

    def test_recovering_flag_while_confirming(self):
        eng, net = sim_engine()
        net.set_state("sim-a", "down") if hasattr(net, "set_state") else None
        v = connectivity_view(eng, time.time())
        self.assertIn("recovering", v["connections"][0])
        self.assertIn("next_probe_in_s", v["connections"][0])


if __name__ == "__main__":
    unittest.main()
