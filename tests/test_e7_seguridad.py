"""E7: pruebas negativas de seguridad contra la API real (loopback, red SIMULADA).

Cada prueba comprueba el EFECTO (la sesión o la marca no cambió), no solo el código HTTP.
No es una prueba de Windows físico.
"""
import glob
import http.client
import json
import os
import socket
import time
import unittest

from helpers import sim_engine
from cauce.api import CauceAPI
from cauce.client import CauceAPIError, CauceClient, CauceSessionClient


class Base(unittest.TestCase):
    def setUp(self):
        self.eng, self.net = sim_engine(sessions={"max": 4})
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.admin = CauceClient(self.api.token, port=self.api.port)
        self.ta = self.admin.create_token("forms-a", "session")
        self.tb = self.admin.create_token("forms-b", "session")
        self.tr = self.admin.create_token("monitor", "read")
        self.a = CauceSessionClient(self.ta["token"], port=self.api.port)
        self.b = CauceSessionClient(self.tb["token"], port=self.api.port)

    def tearDown(self):
        self.api.stop()

    def raw(self, method, path, token=None, body=None, headers=None, raw_body=None, timeout=5):
        h = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=timeout)
        hd = dict(headers or {})
        if token:
            hd["Authorization"] = f"Bearer {token}"
        if body is not None:
            hd["Content-Type"] = "application/json"
            raw_body = json.dumps(body)
        h.request(method, path, body=raw_body, headers=hd)
        r = h.getresponse()
        data = r.read().decode("utf-8")
        h.close()
        return r.status, (json.loads(data) if data.startswith("{") else data)

    def en_curso(self):
        self.a.register("forms-a")
        time.sleep(0.6)
        self.a.heartbeat(operation="in_progress")
        return self.a.session_id


class AccesoCruzadoTest(Base):
    def test_otra_credencial_no_puede_latir_ni_cerrar_ni_leer_mi_sesion(self):
        sid = self.en_curso()
        P = f"/api/v1/sessions/{sid}"
        for m, p, body in (("POST", P + "/heartbeat", {"status": "ok", "operation": "idle"}),
                           ("DELETE", P, None), ("GET", P, None)):
            st, out = self.raw(m, p, self.tb["token"], body)
            self.assertIn(st, (403, 404), f"{m} {p} con otra credencial → {st} {out}")
        v = self.admin._call("GET", P.replace("/api/v1", ""))
        self.assertEqual(v["operation"], "in_progress")
        self.assertFalse(v.get("closed_at"))

    def test_la_credencial_session_no_puede_resolver_su_propia_marca(self):
        sid = self.en_curso()
        time.sleep(0.6)
        self.a.heartbeat(ok=False, error_code="other", operation="in_progress")   # fallo a medias → marca
        self.assertTrue(self.eng.sessions._sessions[sid].requires_human)
        st, out = self.raw("POST", f"/api/v1/sessions/{sid}/resolve", self.ta["token"], {})
        self.assertEqual(st, 403, out)
        st, out = self.raw("POST", f"/api/v1/sessions/{sid}/resolve", self.tr["token"], {})
        self.assertEqual(st, 403, out)
        self.assertTrue(self.eng.sessions._sessions[sid].requires_human)

    def test_read_y_session_no_llegan_a_rutas_admin(self):
        for tok in (self.ta["token"], self.tr["token"]):
            for m, p, body in (("POST", "/api/v1/tokens", {"name": "x", "scope": "admin"}),
                               ("GET", "/api/v1/tokens", None), ("POST", "/api/v1/stop-all", {}),
                               ("POST", "/api/v1/shutdown", {}), ("POST", "/api/v1/selection", {"connection_id": None}),
                               ("POST", "/api/v1/rotation", {"name": "r", "pool": ["a"]})):
                st, _ = self.raw(m, p, tok, body)
                self.assertEqual(st, 403, f"{m} {p}")
        self.assertEqual(len(self.admin.tokens()), 3)

    def test_read_no_registra_sesiones(self):
        st, _ = self.raw("POST", "/api/v1/sessions", self.tr["token"], {"label": "x", "authorized_by_user": True})
        self.assertEqual(st, 403)
        self.assertEqual(len(self.eng.sessions.summary()["sessions"]), 0)


class CredencialTest(Base):
    def test_revocada_a_mitad_de_operacion_no_borra_la_marca(self):
        sid = self.en_curso()
        self.admin.revoke_token(self.ta["id"])
        with self.assertRaises(CauceAPIError) as cm:
            self.a.heartbeat(operation="idle")         # el «idle» no llega: credencial revocada
        self.assertEqual(cm.exception.status, 401)
        v = self.admin._call("GET", f"/sessions/{sid}")
        self.assertEqual(v["operation"], "in_progress")

    def test_credenciales_malformadas(self):
        for hdr in ("Bearer ", "Bearer x", "Basic abc", "Bearer " + "A" * 5000, "Bearer cauce_zz_" + "0" * 40,
                    "Bearer " + self.ta["token"][:-1] + ("0" if self.ta["token"][-1] != "0" else "1")):
            st, _ = self.raw("GET", "/api/v1/status/sessions", headers={"Authorization": hdr})
            self.assertEqual(st, 401, hdr[:30])


class CuerpoYCabecerasTest(Base):
    def test_content_length_negativo_no_cuelga_el_hilo(self):
        """Antes: int('-1') pasa el tope y rfile.read(-1) espera hasta que el cliente cierre."""
        s = socket.create_connection(("127.0.0.1", self.api.port), timeout=3)
        s.sendall((f"POST /api/v1/stop-all HTTP/1.1\r\nHost: 127.0.0.1:{self.api.port}\r\n"
                   f"Authorization: Bearer {self.api.token}\r\nContent-Length: -1\r\n\r\n").encode())
        try:
            data = s.recv(4096)
        except socket.timeout:
            data = b""
        s.close()
        self.assertTrue(data.startswith(b"HTTP/1.0 400") or data.startswith(b"HTTP/1.1 400"),
                        f"respuesta: {data[:60]!r}")

    def test_host_y_origin_ajenos(self):
        st, out = self.raw("GET", "/api/v1/status/sessions", self.tr["token"], headers={"Host": "evil.example"})
        self.assertEqual(st, 421)
        st, out = self.raw("POST", "/api/v1/stop-all", self.api.token, {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    def test_cuerpo_no_objeto_y_enorme(self):
        st, _ = self.raw("POST", "/api/v1/sessions", self.ta["token"], raw_body="[1,2]",
                         headers={"Content-Type": "application/json"})
        self.assertEqual(st, 400)
        st, _ = self.raw("POST", "/api/v1/sessions", self.ta["token"], raw_body="x" * (300 * 1024),
                         headers={"Content-Type": "application/json"})
        self.assertEqual(st, 413)
        self.assertEqual(len(self.eng.sessions.summary()["sessions"]), 0)


class SecretosTest(Base):
    def test_ningun_secreto_en_registros_ni_auditoria(self):
        sid = self.en_curso()
        self.raw("GET", "/api/v1/status/sessions?x=" + self.ta["token"], self.tr["token"])
        self.raw("GET", "/api/v1/status/sessions", "cauce_dead_" + "f" * 40)
        time.sleep(0.6); self.a.heartbeat(ok=False, error_code="network_error", operation="in_progress")
        secretos = [self.api.token, self.ta["token"], self.tb["token"], self.tr["token"]]
        d = self.eng.cfg["data_dir"] if isinstance(self.eng.cfg, dict) else self.eng.cfg.data_dir
        textos = []
        for f in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if os.path.isfile(f) and not f.endswith((".bin", ".dpapi")):
                with open(f, "rb") as fh:
                    textos.append((f, fh.read().decode("utf-8", "replace")))
        for f, t in textos:
            for sec in secretos:
                self.assertNotIn(sec.split(".")[-1], t, f"secreto en {os.path.basename(f)}")
        self.assertTrue(sid)


if __name__ == "__main__":
    unittest.main()
