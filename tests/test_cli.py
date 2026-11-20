"""Pruebas de la línea de órdenes ante un cierre inesperado del motor."""
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import unittest

import helpers  # noqa: F401  (ajusta sys.path)
from cauce.__main__ import main
from cauce.secrets_store import write_secret


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StaleStateTest(unittest.TestCase):
    """Regresión: tras un corte o un proceso terminado a la fuerza quedan api.json y api.token.
    Antes, 'cauce status' terminaba con una traza de error de Python."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cauce-cli-")
        port = free_port()  # nadie escucha en él: simula el motor caído
        with open(os.path.join(self.tmp, "api.json"), "w", encoding="utf-8") as fh:
            json.dump({"port": port, "pid": 999999, "mode": "real", "version": "0.1.0"}, fh)
        write_secret(os.path.join(self.tmp, "api.token"), "token-viejo")
        self.cfg = os.path.join(self.tmp, "c.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"data_dir": self.tmp, "api": {"port": port}}, fh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--config", self.cfg, *args])
        return code, err.getvalue()

    def test_status_stop_panel_with_stale_files(self):
        for cmd in ("status", "stop", "panel"):
            with self.subTest(cmd=cmd):
                code, err = self.run_cli(cmd)
                self.assertEqual(code, 3)
                self.assertIn("no se cerró correctamente", err)
                self.assertNotIn("Traceback", err)

    def test_no_files_at_all(self):
        os.remove(os.path.join(self.tmp, "api.json"))
        code, err = self.run_cli("status")
        self.assertEqual(code, 3)
        self.assertIn("no está en marcha", err)


if __name__ == "__main__":
    unittest.main()
