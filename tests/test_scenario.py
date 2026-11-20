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


if __name__ == "__main__":
    unittest.main()
