"""0.2.0: puerto independiente, rotación apagada con el motor real y versión única."""
import os
import re
import tempfile
import unittest
from unittest import mock

from helpers import sim_engine  # noqa: F401  (añade la raíz al path)

import cauce
from cauce import client as client_mod
from cauce.api import CauceAPI
from cauce.audit import AuditLog
from cauce.client import CauceAPIError, CauceClient
from cauce.config import ConfigError, load
from cauce.engine import Engine
from cauce.policy import RotationDisabled
from cauce.simulator import SimulatedNetwork, three_links

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def real_mode_engine(**lab):
    d = tempfile.mkdtemp(prefix="cauce-test-")
    cfg = load(overrides={"data_dir": d, "health": {"targets": ["sim.diag:443"]},
                          "lab": {"allowed_hosts": ["lab.sim"], **lab}})
    net = SimulatedNetwork(three_links())
    # mode="real" con red simulada: solo para comprobar la regla de rotación sin tocar la red del equipo
    eng = Engine(cfg, net, net, audit=AuditLog(os.path.join(d, "audit.jsonl")), mode="real")
    eng.refresh()
    eng.health.check_all(force=True)
    return eng


class PortTest(unittest.TestCase):
    def test_default_port_does_not_collide_with_forms_monitor_or_labserver(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CAUCE_NE_PORT", None)
            port = load(overrides={"data_dir": tempfile.mkdtemp()})["api"]["port"]
        self.assertEqual(port, 8780)
        # 8765 Forms (interfaz web), 8770 servidor de laboratorio, 8000 Monitor, 8200/8201 portal de laboratorio
        self.assertNotIn(port, (8765, 8770, 8000, 8200, 8201))

    def test_client_copy_keeps_same_default_port(self):
        self.assertEqual(client_mod.DEFAULT_API_PORT, cauce.DEFAULT_API_PORT)

    def test_env_variable_sets_port_without_config_file(self):
        with mock.patch.dict(os.environ, {"CAUCE_NE_PORT": "8790"}):
            self.assertEqual(load(overrides={"data_dir": tempfile.mkdtemp()})["api"]["port"], 8790)
        with mock.patch.dict(os.environ, {"CAUCE_NE_PORT": "no-es-numero"}):
            with self.assertRaises(ConfigError):
                load(overrides={"data_dir": tempfile.mkdtemp()})
        with mock.patch.dict(os.environ, {"CAUCE_NE_PORT": "80"}):
            with self.assertRaises(ConfigError):
                load(overrides={"data_dir": tempfile.mkdtemp()})

    def test_explicit_override_wins_over_env(self):
        with mock.patch.dict(os.environ, {"CAUCE_NE_PORT": "8790"}):
            cfg = load(overrides={"data_dir": tempfile.mkdtemp(), "api": {"port": 8791}})
        self.assertEqual(cfg["api"]["port"], 8791)


class VersionTest(unittest.TestCase):
    def test_single_version_in_package_and_pyproject(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            m = re.search(r'^version = "([^"]+)"', fh.read(), re.M)
        self.assertEqual(m.group(1), cauce.__version__)
        self.assertEqual(cauce.__version__, "0.2.0.dev0")


class RotationOutsideLabTest(unittest.TestCase):
    def test_real_engine_refuses_rotation_by_default(self):
        eng = real_mode_engine()
        self.assertFalse(eng.rotation_enabled)
        with self.assertRaises(RotationDisabled):
            eng.rotation.create("p", ["sim-a", "sim-b"], interval_s=10)
        self.assertEqual(eng.rotation.all(), [])
        self.assertFalse(eng.diagnostics()["rotation_enabled"])

    def test_real_engine_api_answers_403_rotation_disabled(self):
        eng = real_mode_engine()
        api = CauceAPI(eng, "127.0.0.1", 0)
        api.start()
        try:
            c = CauceClient(api.token, port=api.port)
            with self.assertRaises(CauceAPIError) as cm:
                c.create_rotation("p", ["sim-a", "sim-b"], 10, 5)
            self.assertEqual((cm.exception.status, cm.exception.code), (403, "rotation_disabled"))
        finally:
            api.stop()

    def test_explicit_lab_flag_enables_it(self):
        eng = real_mode_engine(rotation_enabled=True)
        self.assertTrue(eng.rotation_enabled)
        eng.rotation.create("p", ["sim-a", "sim-b"], interval_s=10)

    def test_flag_must_be_boolean(self):
        with self.assertRaises(ConfigError):
            load(overrides={"data_dir": tempfile.mkdtemp(), "lab": {"rotation_enabled": "yes"}})

    def test_simulator_keeps_rotation_for_the_scenario(self):
        eng, _ = sim_engine()
        self.assertTrue(eng.rotation_enabled)


if __name__ == "__main__":
    unittest.main()


class FieldPackageFilesTest(unittest.TestCase):
    """Los .cmd/.ps1 del paquete físico: ASCII y CRLF (cmd.exe en español falla con acentos en un .bat)."""

    def test_scripts_are_ascii_crlf_and_never_touch_network_config(self):
        d = os.path.join(ROOT, "scripts", "windows", "validacion_fisica")
        names = sorted(os.listdir(d))
        self.assertEqual(names, ["SERVIDOR_LABORATORIO.cmd", "TRES_MOTORES.ps1", "VALIDAR_DOS_ENLACES.cmd"])
        forbidden = ("route add", "route delete", "set-netipinterface", "new-netroute", "remove-netroute",
                     "netsh", "disable-netadapter", "enable-netadapter", "set-netroute", "new-netfirewallrule")
        for n in names:
            raw = open(os.path.join(d, n), "rb").read()
            raw.decode("ascii")
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), n)
            low = raw.decode("ascii").lower()
            for f in forbidden:
                self.assertNotIn(f, low, f"{n} contiene {f}")
