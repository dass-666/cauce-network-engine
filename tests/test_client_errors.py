"""Clasificación de errores de ``CauceReadClient.connectivity_or_unknown`` (solo simulación y loopback).

401/403/400 NO se disfrazan de «sin información» y detienen el cliente; 5xx y motor apagado → ``usable=None``;
motor sano con conectividad no disponible → estado real. La compatibilidad con el consumidor de ejemplo
(examples/consumidor_lectura.py, PR #6) se comprueba solo si el archivo existe en el árbol.
"""
import os
import sys
import unittest
from unittest import mock

from helpers import sim_engine
from test_read_tokens import Base
from cauce.api import CauceAPI
from cauce.client import CauceAPIError, CauceReadClient

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
HAVE_EXAMPLE = os.path.exists(os.path.join(EX, "consumidor_lectura.py"))


class ClientErrorTest(Base):
    def requests_seen(self):
        with open(self.api.access._path, encoding="utf-8") as fh:
            return len(fh.read().splitlines())

    def test_healthy_engine_keeps_real_state_including_unavailable(self):
        self.assertEqual(self.read.connectivity_or_unknown()["state"], "usable")
        for cid in ("sim-a", "sim-b"):
            self.net.set_state(cid, "disconnected")
        self.eng.refresh()
        v = self.read.connectivity_or_unknown()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unavailable", False, "no_active_links"))
        self.assertIsNone(self.read.halted)

    def test_engine_off_is_unknown_and_not_halted(self):
        self.api.stop()
        v = self.read.connectivity_or_unknown()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("engine_unreachable", None, "engine_unreachable"))
        self.assertIsNone(self.read.halted)

    def test_503_is_unknown_with_code_and_not_halted(self):
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("disco lleno")):
            with self.assertRaises(CauceAPIError):
                self.admin.revoke_token(self.read_id)
        v = self.read.connectivity_or_unknown()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("engine_unreachable", None, "credentials_locked"))
        self.assertIsNone(self.read.halted)

    def test_5xx_generic_is_unknown(self):
        with mock.patch("cauce.api.connectivity_view", side_effect=RuntimeError("boom")):
            v = self.read.connectivity_or_unknown()
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("engine_unreachable", None, "internal_error"))

    def test_401_revoked_raises_halts_and_stops_sending_requests(self):
        self.admin.revoke_token(self.read_id)
        with self.assertRaises(CauceAPIError) as cm:
            self.read.connectivity_or_unknown()
        self.assertEqual((cm.exception.status, cm.exception.code), (401, "token_revoked"))
        self.assertIs(self.read.halted, cm.exception)
        n = self.requests_seen()
        for _ in range(5):
            with self.assertRaises(CauceAPIError):
                self.read.connectivity_or_unknown()
        self.assertEqual(self.requests_seen(), n, "detenido: cero peticiones")

    def test_401_invalid_token(self):
        c = CauceReadClient("cr1.deadbeef." + "z" * 43, port=self.api.port)
        with self.assertRaises(CauceAPIError) as cm:
            c.connectivity_or_unknown()
        self.assertEqual((cm.exception.status, cm.exception.code), (401, "unauthorized"))
        self.assertIsNotNone(c.halted)

    def test_403_raises_and_halts(self):
        routes = [(m, p, f, "admin" if p.pattern.endswith("connectivity$") else n) for m, p, f, n in self.api.routes]
        with mock.patch.object(self.api, "routes", routes):
            with self.assertRaises(CauceAPIError) as cm:
                self.read.connectivity_or_unknown()
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "insufficient_scope"))
        n = self.requests_seen()
        with self.assertRaises(CauceAPIError):
            self.read.connectivity_or_unknown()
        self.assertEqual(self.requests_seen(), n)

    def test_400_raises_and_halts(self):
        with mock.patch.object(self.read._c, "_call", side_effect=CauceAPIError(400, "unsupported_parameter", "x")):
            with self.assertRaises(CauceAPIError):
                self.read.connectivity_or_unknown()
        self.assertIsNotNone(self.read.halted)

    def test_other_4xx_raises_without_halting(self):
        with mock.patch.object(self.read._c, "_call", side_effect=CauceAPIError(404, "not_found", "x")):
            with self.assertRaises(CauceAPIError):
                self.read.connectivity_or_unknown()
        self.assertIsNone(self.read.halted)
        self.assertEqual(self.read.connectivity_or_unknown()["state"], "usable")

    def test_reset_after_operator_fixes_credential(self):
        self.admin.revoke_token(self.read_id)
        with self.assertRaises(CauceAPIError):
            self.read.connectivity_or_unknown()
        new = self.admin.create_token("monitor2")["token"]
        self.read._c.token = new
        with self.assertRaises(CauceAPIError):
            self.read.connectivity_or_unknown()      # sigue detenido hasta reset(): decisión explícita del operador
        self.read.reset()
        self.assertEqual(self.read.connectivity_or_unknown()["state"], "usable")

    def test_restart_keeps_working_and_epoch_changes(self):
        v1 = self.read.connectivity_or_unknown()
        port, d = self.api.port, self.eng.cfg["data_dir"]
        self.api.stop()
        self.assertEqual(self.read.connectivity_or_unknown()["state"], "engine_unreachable")
        eng2, _ = sim_engine(data_dir=d)
        self.api = CauceAPI(eng2, "127.0.0.1", port)
        self.api.start()
        v2 = self.read.connectivity_or_unknown()
        self.assertEqual(v2["state"], "usable")
        self.assertNotEqual(v1["engine"]["epoch"], v2["engine"]["epoch"], "el reinicio es visible para el consumidor")


@unittest.skipUnless(HAVE_EXAMPLE, "examples/consumidor_lectura.py llega con el PR #6")
class ExampleCompatibilityTest(Base):
    """El cliente compartido y el consumidor de ejemplo deben llegar a la misma conclusión ante cada error."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, EX)
        from consumidor_lectura import StatusConsumer
        self.Consumer = StatusConsumer

    def tearDown(self):
        sys.path.remove(EX)
        super().tearDown()

    def both(self):
        client = CauceReadClient(self.read_token, port=self.api.port, timeout=1.0)
        cons = self.Consumer(CauceReadClient(self.read_token, port=self.api.port, timeout=1.0))
        out = {}
        try:
            v = client.connectivity_or_unknown()
            out["client"] = ("info", v["state"], v["usable"])
        except CauceAPIError as e:
            out["client"] = ("halt", e.code, None)
        r = cons.poll()
        out["consumer"] = ("halt", cons.halted, None) if cons.halted else ("info", r["state"], r["usable"])
        return out

    def assertCompatible(self, out):
        c, k = out["client"], out["consumer"]
        self.assertEqual(c[0] == "halt", k[0] == "halt", out)
        if c[0] == "halt":
            self.assertEqual(c[1], k[1], out)
        else:
            self.assertEqual(c[2], k[2], out)                      # mismo usable
            self.assertEqual(c[1] == "engine_unreachable", k[1] == "no_info", out)

    def test_same_conclusion_for_every_error(self):
        out = self.both()
        self.assertCompatible(out)
        self.assertEqual(out["client"][1:], ("usable", True))
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("disco lleno")):
            with self.assertRaises(CauceAPIError):
                self.admin.revoke_token(self.read_id)
        out = self.both()                                           # 503
        self.assertCompatible(out)
        self.assertEqual(out["client"][2], None)
        self.admin.recover_tokens()
        out = self.both()                                           # 401 revocada
        self.assertCompatible(out)
        self.assertEqual(out["client"][:2], ("halt", "token_revoked"))
        self.api.stop()
        out = self.both()                                           # 401 no se puede comprobar con motor apagado: ambos «sin información»
        self.assertCompatible(out)
        self.assertEqual(out["client"][1], "engine_unreachable")

    def test_same_conclusion_403_and_engine_off(self):
        routes = [(m, p, f, "admin" if p.pattern.endswith("connectivity$") else n) for m, p, f, n in self.api.routes]
        with mock.patch.object(self.api, "routes", routes):
            out = self.both()
        self.assertCompatible(out)
        self.assertEqual(out["client"][:2], ("halt", "insufficient_scope"))
        self.api.stop()
        self.assertCompatible(self.both())

    def test_same_conclusion_unavailable_is_real_state(self):
        for cid in ("sim-a", "sim-b"):
            self.net.set_state(cid, "disconnected")
        self.eng.refresh()
        out = self.both()
        self.assertCompatible(out)
        self.assertEqual(out["client"], ("info", "unavailable", False))
        self.assertEqual(out["consumer"], ("info", "unavailable", False))


if __name__ == "__main__":
    unittest.main()
