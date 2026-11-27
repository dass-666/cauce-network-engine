"""Paquete de validación física: el guion completo con el simulador (no es validación física)."""
import json
import os
import tempfile
import time
import unittest
import urllib.request

from helpers import sim_engine  # noqa: F401  (añade la raíz al path)

from cauce.fieldcheck import FAIL, OK, mask_ip, run
from cauce.labserver import LabServer


class FieldcheckSimulatedTest(unittest.TestCase):
    def test_full_script_passes_and_is_labelled_simulated(self):
        out = tempfile.mkdtemp()
        res = run("http://lab.sim:8770", out, simulate=True, ask=lambda _m: None, steps=60, step_delay_s=0.02,
                  engines={})
        self.assertEqual(res["failed"], [], res)
        self.assertTrue(res["evidence_type"].startswith("SIMULADA"))
        by = {c["id"]: c for c in res["checks"]}
        for cid in ("F2", "F3", "F5", "F6", "F7", "F8", "F10", "F12"):
            self.assertEqual(by[cid]["status"], OK, cid)
        f8 = by["F8"]["evidence"]
        self.assertEqual(f8["duplicates"], {})
        self.assertEqual(f8["lost"], [])
        kinds = [h.get("kind") for h in f8["lease_history"]]
        self.assertEqual(kinds[1:], ["failover", "failback"])
        self.assertFalse(res["meta"]["global_routes_modified_by_engine"])
        md = open(os.path.join(out, "fieldcheck.md"), encoding="utf-8").read()
        self.assertIn("SIMULADA", md)

    def test_report_never_contains_full_ips(self):
        out = tempfile.mkdtemp()
        res = run("http://lab.sim:8770", out, simulate=True, ask=lambda _m: None, steps=20, step_delay_s=0.01,
                  engines={})
        raw = json.dumps(res)
        for ip in ("198.51.100.10", "203.0.113.20"):
            self.assertNotIn(ip, raw)
        self.assertEqual(mask_ip("203.0.113.20"), "203.0.113.x")

    def test_lab_url_must_be_http(self):
        with self.assertRaises(SystemExit):
            run("ftp://x", tempfile.mkdtemp(), simulate=True, ask=lambda _m: None)


class LabSessionPageTest(unittest.TestCase):
    def test_beats_gaps_reloads_and_writes_by_op(self):
        now = [1000.0]
        srv = LabServer(port=0, clock=lambda: now[0]).start()
        base = f"http://127.0.0.1:{srv.port}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            page = opener.open(base + "/session?sid=s1").read().decode()
            self.assertIn("setInterval(beat, 1000)", page)
            for n, t in ((1, 1000), (2, 1001), (3, 1002), (9, 1008), (10, 1009), (1, 1010), (2, 1011)):
                now[0] = t
                opener.open(f"{base}/beat?sid=s1&n={n}").read()
            r = srv.session_report("s1")
            self.assertEqual(r["beats"], 7)
            self.assertEqual(len(r["gaps"]), 1)
            self.assertEqual((r["gaps"][0]["seconds"], r["gaps"][0]["beats_lost"]), (6.0, 5))
            self.assertEqual(r["page_reloads"], 1)
            for _ in range(2):
                opener.open(urllib.request.Request(base + "/write?op=abc", data=b"x", method="POST")).read()
            self.assertEqual(srv.writes["op-abc"], 2)
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
