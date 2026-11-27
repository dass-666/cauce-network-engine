"""E5: servidor de validación PROPIO y privado (cauce/labserver.py). Solo 127.0.0.1 en estas pruebas."""
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

from cauce.labserver import MAX_REQUESTS_KEPT, LabServer, LabServerError, check_bind

CLAVE = "clave-ficticia-de-pruebas-1234567890"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, data=None):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class PoliticaTest(unittest.TestCase):
    def test_nunca_en_direccion_publica(self):
        for host in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
            with self.assertRaises(LabServerError, msg=host):
                check_bind(host, CLAVE)

    def test_red_local_o_tailscale_exige_clave(self):
        for host in ("192.168.1.50", "10.0.0.5", "100.101.102.103", "0.0.0.0"):
            with self.assertRaises(LabServerError, msg=host):
                check_bind(host, None)
            with self.assertRaises(LabServerError, msg=host):
                check_bind(host, "corta")
            check_bind(host, CLAVE)

    def test_loopback_sin_clave_sigue_valiendo(self):
        check_bind("127.0.0.1", None)

    def test_nombre_en_vez_de_ip_se_rechaza(self):
        with self.assertRaises(LabServerError):
            check_bind("mi-servidor.example", CLAVE)


class ClaveTest(unittest.TestCase):
    def setUp(self):
        self.srv = LabServer(port=0, clave=CLAVE).start()
        self.raiz = f"http://127.0.0.1:{self.srv.port}"
        self.base = f"{self.raiz}/{CLAVE}"

    def tearDown(self):
        self.srv.stop()

    def test_sin_clave_404_y_nada_contado(self):
        for url in (self.raiz + "/ok", self.raiz + "/stats", f"{self.raiz}/{CLAVE}x/ok", f"{self.raiz}/{CLAVE[:-1]}/ok"):
            self.assertEqual(get(url)[0], 404, url)
        self.assertEqual(get(self.raiz + "/write?op=x", b"x")[0], 404)
        self.assertEqual((self.srv.writes, self.srv.requests_total, self.srv.rejected), ({}, 0, 5))

    def test_con_clave_todas_las_rutas(self):
        self.assertEqual(get(self.base + "/ok")[0], 200)
        self.assertEqual(get(self.base + "/limit")[0], 429)
        for _ in range(2):
            get(self.base + "/write?op=abc", b"x")
        self.assertEqual(self.srv.writes["op-abc"], 2)
        page = get(self.base + "/session?sid=s1")[1]
        self.assertIn("fetch('beat?sid='", page, "latido relativo: funciona bajo /<clave>/")
        get(self.base + "/beat?sid=s1&n=1")
        self.assertEqual(self.srv.session_report("s1")["beats"], 1)

    def test_la_clave_no_queda_en_los_registros(self):
        get(self.base + f"/ok?k={CLAVE}")
        self.assertTrue(self.srv.requests)
        self.assertNotIn(CLAVE, repr(list(self.srv.requests)))


class LimitesTest(unittest.TestCase):
    def test_registros_y_escrituras_acotados(self):
        srv = LabServer(port=0).start()
        try:
            for i in range(MAX_REQUESTS_KEPT + 50):
                srv.requests.append({"i": i})
            self.assertEqual(len(srv.requests), MAX_REQUESTS_KEPT)
        finally:
            srv.stop()


class CliTest(unittest.TestCase):
    def test_cli_rechaza_publica_y_crea_clave_privada(self):
        p = subprocess.run([sys.executable, "-m", "cauce", "labserver", "--host", "1.1.1.1", "--port", "0"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("pública", p.stderr)
        p = subprocess.run([sys.executable, "-m", "cauce", "labserver", "--host", "10.255.255.1", "--port", "0"],
                           cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 2, "red local sin clave: no arranca")


if __name__ == "__main__":
    unittest.main()
