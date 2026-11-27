import tempfile
import unittest

import helpers  # noqa: F401
from cauce.scenario import run


class ScenarioTest(unittest.TestCase):
    def test_ten_step_scenario(self):
        r = run(tempfile.mkdtemp(), quiet=True)
        failed = [(s["n"], c["label"]) for s in r["steps"] for c in s["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual([s["n"] for s in r["steps"]], list(range(1, 11)))
        self.assertIn("SIMULACIÓN", r["warning"])


class SessionScenarioTest(unittest.TestCase):
    def test_two_browser_sessions_scenario(self):
        from cauce.session_scenario import run as run_sessions
        r = run_sessions(tempfile.mkdtemp(), quiet=True)
        failed = [(s["n"], c["label"]) for s in r["steps"] for c in s["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(r["mode"], "SIMULACIÓN")

    def test_scenario_detects_broken_isolation(self):
        """Prueba de la prueba: si el estado de red de una sesión ignorase SU conexión, el escenario debe fallar."""
        from unittest import mock
        from cauce.engine import Engine
        from cauce.session_scenario import run as run_sessions
        orig = Engine._session_network_state
        with mock.patch.object(Engine, "_session_network_state", lambda self, c: orig(self, "sim-a")):
            r = run_sessions(None, quiet=True)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
