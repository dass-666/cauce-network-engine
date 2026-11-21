"""Validación de un CONSUMIDOR EXTERNO de laboratorio con credencial ``read`` (examples/consumidor_lectura.py).

Todo corre contra el simulador y sockets de loopback: NO es una prueba de hardware ni de Windows físico, y
NO se ha probado con Cauce Monitor real. Reutiliza ``Base`` de test_read_tokens.py (PR #5); aquí solo lo que faltaba.
"""
import json
import logging
import os
import re
import sys
import time
import unittest
from unittest import mock

from helpers import sim_engine
from test_read_tokens import Base, SIM_SECRETS
from cauce.api import CauceAPI
from cauce.client import CauceClient, CauceReadClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples"))
from consumidor_lectura import KILL_ENV, MIN_INTERVAL_S, StatusConsumer  # noqa: E402

IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")


class ConsumerBase(Base):
    def setUp(self):
        super().setUp()
        self.cons = StatusConsumer(self.read, interval_s=5, follow_events=True)

    def access_rows(self):
        with open(self.api.access._path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh.read().splitlines()]

    def consumer_requests(self):
        return [r for r in self.access_rows() if r["who"] == "monitor"]


class ConsumerHappyAndErrorsTest(ConsumerBase):
    def test_uses_only_read_credential_and_only_status_paths(self):
        for _ in range(3):
            self.assertEqual(self.cons.poll()["state"], "usable")
        mine = self.consumer_requests()
        self.assertGreaterEqual(len(mine), 6)
        self.assertTrue(all(r["status"] == 200 for r in mine))
        self.assertTrue(all(r["path"].split("?")[0] in ("/api/v1/status/connectivity", "/api/v1/status/events") for r in mine))
        admin_rows = [r for r in self.access_rows() if r["who"] not in ("monitor", None)]
        self.assertEqual([(r["method"], r["path"]) for r in admin_rows], [("POST", "/api/v1/tokens")],
                         "lo único hecho con admin fue crear la credencial (setUp); el consumidor no lo usó")
        self.assertNotIn(self.api.token, self.read_token)

    def test_unknown_is_null_and_does_not_become_false(self):
        self.eng.health._state.clear()
        r = self.cons.poll()
        self.assertEqual((r["state"], r["usable"], r["detail"]), ("unknown", None, "insufficient_measurements"))
        self.assertIsNot(r["usable"], False)

    def test_engine_off_is_no_info_with_backoff_then_recovers(self):
        port = self.api.port
        self.assertTrue(self.cons.poll()["usable"])
        self.api.stop()
        delays = []
        for _ in range(4):
            r = self.cons.poll()
            self.assertEqual((r["state"], r["usable"], r["detail"]), ("no_info", None, "engine_unreachable"))
            delays.append(self.cons.next_delay())
        self.assertEqual(delays, [10, 20, 40, 60], "espera creciente y con tope")
        # el motor vuelve (mismo puerto y mismo directorio de datos: las credenciales sobreviven)
        eng2, net2 = sim_engine(data_dir=self.eng.cfg["data_dir"])
        self.api = CauceAPI(eng2, "127.0.0.1", port)
        self.api.start()
        r = self.cons.poll()
        self.assertEqual((r["state"], r["usable"]), ("usable", True))
        self.assertEqual(self.cons.failures, 0)
        self.assertEqual(self.cons.next_delay(), 5)

    def test_engine_restart_detected_by_epoch_even_when_seq_is_higher(self):
        for _ in range(30):
            self.eng.audit.record("health.change", "x", connection_id="sim-a", old="healthy", new="degraded")
        self.cons.poll()
        old_epoch, old_since = self.cons.epoch, self.cons.since
        port = self.api.port
        self.api.stop()
        eng2, _ = sim_engine(data_dir=self.eng.cfg["data_dir"])
        for _ in range(60):                     # el motor nuevo YA superó el last_seq anterior: solo el epoch delata el reinicio
            eng2.audit.record("health.change", "y", connection_id="sim-a", old="healthy", new="degraded")
        self.api = CauceAPI(eng2, "127.0.0.1", port)
        self.api.start()
        r = self.cons.poll()
        self.assertEqual(r["state"], "usable")
        self.assertEqual(self.cons.restarts, 1)
        self.assertNotEqual(self.cons.epoch, old_epoch)
        self.assertEqual(self.cons.since, eng2.audit.last_seq)
        self.assertGreater(eng2.audit.last_seq, old_since)

    def test_event_loss_triggers_resync_not_silence(self):
        from collections import deque
        a = self.eng.audit
        self.cons.poll()
        a._events = deque(a._events, maxlen=20)
        for _ in range(60):
            a.record("health.change", "x", connection_id="sim-a", old="healthy", new="degraded")
        r = self.cons.poll()
        self.assertEqual(self.cons.resyncs, 1)
        self.assertEqual(self.cons.since, a.last_seq)
        self.assertEqual(r["state"], "usable")
        self.cons.poll()
        self.assertEqual(self.cons.resyncs, 1, "tras resincronizar no vuelve a pedir resincronización")

    def test_revoked_credential_halts_and_stops_hitting_the_engine(self):
        self.assertTrue(self.cons.poll()["usable"])
        self.admin.revoke_token(self.read_id)
        r = self.cons.poll()
        self.assertEqual((r["state"], r["usable"], r["detail"]), ("no_info", None, "token_revoked"))
        n = len(self.consumer_requests()) + len([x for x in self.access_rows() if x["status"] == 401])
        for _ in range(5):
            self.assertEqual(self.cons.poll()["detail"], "token_revoked")
        n2 = len(self.consumer_requests()) + len([x for x in self.access_rows() if x["status"] == 401])
        self.assertEqual(n, n2, "detenido: cero peticiones adicionales")

    def test_garbage_credential_halts_as_unauthorized(self):
        cons = StatusConsumer(CauceReadClient("cr1.deadbeef." + "z" * 43, port=self.api.port))
        self.assertEqual(cons.poll()["detail"], "unauthorized")
        self.assertEqual(cons.halted, "unauthorized")

    def test_locked_store_503_is_no_info_not_no_internet_and_not_halted(self):
        with mock.patch("cauce.tokens.write_secret", side_effect=OSError("disco lleno")):
            self.admin_revoke_fails()
        r = self.cons.poll()
        self.assertEqual((r["state"], r["usable"], r["detail"]), ("no_info", None, "credentials_locked"))
        self.assertIsNone(self.cons.halted, "503 es transitorio: se reintenta con espera creciente")
        self.assertEqual(self.cons.failures, 1)
        # recuperación por el operador: la credencial sana ya no lo es porque se intentó revocar esta → 401 revocada
        self.admin.recover_tokens()
        self.assertEqual(self.cons.poll()["detail"], "token_revoked")

    def admin_revoke_fails(self):
        from cauce.client import CauceAPIError
        with self.assertRaises(CauceAPIError):
            self.admin.revoke_token(self.read_id)

    def test_forbidden_scope_halts(self):
        """Si alguien cambiara la ruta a admin, el consumidor se detiene en vez de insistir."""
        with mock.patch.object(self.api, "routes", [(m, p, f, "admin" if p.pattern.endswith("connectivity$") else n)
                                                  for m, p, f, n in self.api.routes]):
            r = self.cons.poll()
        self.assertEqual((r["state"], r["usable"], r["detail"]), ("no_info", None, "insufficient_scope"))
        self.assertEqual(self.cons.halted, "insufficient_scope")

    def test_polling_interval_has_a_floor(self):
        self.assertEqual(StatusConsumer(self.read, interval_s=0.1).interval_s, MIN_INTERVAL_S)
        self.assertEqual(StatusConsumer(self.read, interval_s=30).interval_s, 30)


class KillSwitchTest(ConsumerBase):
    def test_env_and_file_switch_make_zero_requests_and_never_raise(self):
        before = len(self.access_rows())
        with mock.patch.dict(os.environ, {KILL_ENV: "1"}):
            self.assertEqual(self.cons.poll(), {"state": "disabled", "usable": None, "detail": "kill_switch"})
        flag = os.path.join(self.eng.cfg["data_dir"], "monitor.off")
        cons = StatusConsumer(self.read, kill_file=flag)
        with open(flag, "w") as fh:
            fh.write("")
        self.assertEqual(cons.poll()["state"], "disabled")
        self.assertEqual(before, len(self.access_rows()), "desactivado = cero peticiones")
        self.api.stop()                                   # aunque el motor esté apagado, desactivado no falla ni espera
        self.assertEqual(cons.poll()["state"], "disabled")
        self.assertEqual(cons.failures, 0)
        os.remove(flag)


class ConsumerLogsTest(ConsumerBase):
    def test_logs_never_contain_ip_mac_or_tokens_across_all_scenarios(self):
        with self.assertLogs("cauce.consumer", level=logging.DEBUG) as cm:
            self.cons.poll()
            self.net.set_state("sim-b", "disconnected")
            self.eng.refresh()
            self.cons.poll()
            self.eng.health._state.clear()
            self.cons.poll()                                          # unknown
            self.api.stop()
            self.cons.poll()                                          # motor apagado: el texto del OSError podría llevar el puerto
            self.cons.poll()
            bad = StatusConsumer(CauceReadClient("cr1.deadbeef." + "z" * 43, port=1))
            bad.poll()
            logging.getLogger("cauce.consumer").info("fin")           # garantiza al menos una línea
        text = "\n".join(cm.output)
        self.assertIn("estado=", text)
        self.assertEqual(IP_RE.findall(text), [])
        self.assertEqual(MAC_RE.findall(text), [])
        for s in SIM_SECRETS + [self.read_token, self.read_token.split(".")[2], self.api.token, str(self.api.port)]:
            self.assertNotIn(s, text)

    def test_result_dict_has_no_machine_data(self):
        self.net.set_state("sim-b", "disconnected")
        self.eng.refresh()
        text = json.dumps(self.cons.poll())
        self.assertEqual(IP_RE.findall(text), [])
        self.assertEqual(MAC_RE.findall(text), [])


class ReadConsumerCannotDoAdminTest(Base):
    """Fase 3: un consumidor con credencial read intenta, usando el cliente administrativo COMPLETO, cada acción prohibida.
    Se comprueba el código 403 y que el efecto no ocurrió. (La cobertura exhaustiva de rutas está en
    RevisionPr45Test.test_every_registered_route_is_denied_to_read_unless_whitelisted.)"""

    def test_each_forbidden_action_is_denied_and_has_no_effect(self):
        from cauce.client import CauceAPIError
        evil = CauceClient(self.read_token, port=self.api.port)
        eng = self.eng
        probes = eng.health.snapshot()["sim-a"]["probes"]
        refreshes = []
        orig = eng.refresh
        eng.refresh = lambda *a, **k: (refreshes.append(1), orig(*a, **k))[1]
        actions = {
            "crear rutas": lambda: evil.request_route(owner="intruso", purpose="x"),
            "seleccionar conexiones": lambda: evil.select("sim-b"),
            "rotar salidas (crear)": lambda: evil.create_rotation("r", ["sim-a", "sim-b"]),
            "rotar salidas (activar)": lambda: evil.set_rotation("r", "activate"),
            "ejecutar tareas": lambda: evil.start_task({"kind": "http", "url": "http://127.0.0.1/"}),
            "detener tareas": lambda: evil.stop_all("x"),
            "iniciar sondeos": lambda: evil._call("POST", "/connections/sim-a/check", {}),
            "refrescar interfaces": lambda: evil.connections(refresh=True),
            "detener el motor": lambda: evil.shutdown(),
            "crear credencial admin": lambda: evil.create_token("intruso", "admin"),
            "crear credencial read": lambda: evil.create_token("intruso2"),
            "listar credenciales": lambda: evil.tokens(),
            "revocar credenciales": lambda: evil.revoke_token(self.read_id),
            "leer datos de red (MAC/IP)": lambda: evil.connections(),
        }
        for name, fn in actions.items():
            with self.assertRaises(CauceAPIError, msg=name) as cm:
                fn()
            self.assertEqual((cm.exception.status, cm.exception.code), (403, "insufficient_scope"), name)
        self.assertEqual(refreshes, [])
        self.assertEqual(eng.health.snapshot()["sim-a"]["probes"], probes)
        self.assertFalse(self.api.shutdown_requested.is_set())
        self.assertIsNone(eng.routes.operator_choice)
        self.assertEqual((eng.routes.leases(), eng.executor.all(), eng.rotation.all()), ([], [], []))
        self.assertEqual([t["name"] for t in self.admin.tokens()], ["monitor"])
        self.assertEqual(self.admin.connections()[0]["id"], "sim-a", "el motor sigue funcionando")


if __name__ == "__main__":
    unittest.main()
