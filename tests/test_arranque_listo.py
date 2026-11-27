"""Regresión: cuando aparece api.token, el motor ya está listo para la línea de órdenes.

Antes api.token se escribía antes que api.json y antes de que la API atendiera. Un proceso que esperaba a
que existiera api.token (pruebas de Monitor, laboratorio integrado) podía lanzar `cauce token create` en
esa ventana: el CLI no encontraba api.json, usaba el puerto por defecto y decía «El motor no está en
marcha». Se vio en la CI de Monitor (Red lectura · ubuntu-latest) el 2026-11-27.

La ventana es de microsegundos: esta prueba no la reproduce de forma determinista con el código viejo
(pasó en local), pero sí comprueba el contrato nuevo: api.token aparece solo con api.json y la API lista."""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import helpers  # noqa: F401  (ajusta sys.path)
from cauce.secrets_store import read_secret, write_secret

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ArranqueListoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cauce-listo-")
        self.port = free_port()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"data_dir": self.tmp, "api": {"port": self.port}}, fh)
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_api_token_aparece_con_api_json_y_la_api_responde(self):
        # Restos de una ejecución anterior que no se cerró: no deben tomarse como «listo».
        with open(os.path.join(self.tmp, "api.json"), "w", encoding="utf-8") as fh:
            json.dump({"port": free_port(), "pid": 999999}, fh)
        write_secret(os.path.join(self.tmp, "api.token"), "token-viejo")
        self.proc = subprocess.Popen([sys.executable, "-m", "cauce", "--config", self.cfg, "sim",
                                      "--port", str(self.port)], cwd=RAIZ,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        token_path = os.path.join(self.tmp, "api.token")
        limite = time.time() + 30
        while time.time() < limite:   # sondeo agresivo, como el de quien espera al motor
            try:
                if read_secret(token_path) != "token-viejo":
                    break
            except OSError:
                pass
            time.sleep(0.005)
        else:
            self.fail("el motor no escribió api.token")
        with open(os.path.join(self.tmp, "api.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["port"], self.port)
        r = subprocess.run([sys.executable, "-m", "cauce", "--config", self.cfg, "token", "create",
                            "--name", "prueba", "--out", os.path.join(self.tmp, "p.token")],
                           cwd=RAIZ, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
