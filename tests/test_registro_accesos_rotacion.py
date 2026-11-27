"""E6: access.jsonl no crece sin límite: rota al pasar max_bytes y conserva como mucho `keep` archivos."""
import os
import tempfile
import unittest

import helpers  # noqa: F401  (añade la raíz del repo a sys.path)
from cauce.tokens import AccessLog


class RotacionAccesosTest(unittest.TestCase):
    def test_rota_y_acota_el_espacio(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "access.jsonl")
            log = AccessLog(p, max_bytes=2000, keep=3)
            for i in range(2000):
                log.record(None, "GET", f"/api/v1/status/connectivity?i={i}", 200)
            files = sorted(f for f in os.listdir(d) if f.startswith("access.jsonl"))
            self.assertEqual(files, ["access.jsonl", "access.jsonl.1", "access.jsonl.2", "access.jsonl.3"])
            total = sum(os.path.getsize(os.path.join(d, f)) for f in files)
            self.assertLess(total, 4 * 2000 + 4 * 200, "como mucho keep+1 archivos de ~max_bytes")

    def test_valores_por_omision_acotan_a_unos_20_mb(self):
        log = AccessLog(None)
        self.assertEqual((log._max, log._keep), (5_000_000, 3))


if __name__ == "__main__":
    unittest.main()
