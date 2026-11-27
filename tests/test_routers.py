"""Contrato de observación de routers: solo lectura, sin datos sensibles, «no sé» ≠ «caído»."""
import json
import os
import tempfile
import unittest

from helpers import sim_engine

from cauce.api import CauceAPI
from cauce.client import CauceClient, CauceReadClient
from cauce.config import ConfigError, load
from cauce.routers import (ADAPTERS, MikroTikObserver, OpenWrtObserver, ReadOnlyViolation, RouterWatch,
                           build_observers)
from cauce.routers.simulated import SimulatedRest, SimulatedUbus, scenarios
from cauce.audit import AuditLog


def _observer(vendor, scenario="normal"):
    obs_cls, tr_cls = ADAPTERS[vendor]
    tr = tr_cls(scenario)
    return obs_cls("r1", tr), tr


class AdapterBehaviourTest(unittest.TestCase):
    """Mismos escenarios para cada fabricante: el contrato no depende del proveedor."""

    def test_normal_wan1_active(self):
        for vendor in ADAPTERS:
            with self.subTest(vendor=vendor):
                snap = _observer(vendor)[0].observe()
                self.assertEqual(snap.state, "usable")
                self.assertEqual(len(snap.wans), 2)
                self.assertTrue(all(w.internet == "online" for w in snap.wans))
                self.assertEqual(snap.active_wan, snap.wans[0].label)
                self.assertTrue(all(w.latency_ms and w.latency_ms > 0 for w in snap.wans))

    def test_wan1_down_router_moved_to_wan2(self):
        for vendor in ADAPTERS:
            with self.subTest(vendor=vendor):
                snap = _observer(vendor, "wan1_down")[0].observe()
                self.assertEqual(snap.state, "usable")
                first, second = snap.wans
                self.assertEqual(first.internet, "offline")
                self.assertEqual(second.internet, "online")
                self.assertEqual(snap.active_wan, second.label)

    def test_both_down_is_unavailable(self):
        for vendor in ADAPTERS:
            with self.subTest(vendor=vendor):
                snap = _observer(vendor, "both_down")[0].observe()
                self.assertEqual((snap.state, snap.reason), ("unavailable", "all_wans_offline"))

    def test_garbage_or_silence_is_unknown_never_offline(self):
        for vendor in ADAPTERS:
            with self.subTest(vendor=vendor):
                obs, tr = _observer(vendor, "garbage")
                self.assertEqual(obs.observe().state, "unknown")
                tr.offline = True
                snap = obs.observe()
                self.assertEqual((snap.state, snap.reason), ("unknown", "router_unreachable"))
                self.assertEqual(snap.wans, [])

    def test_mikrotik_text_values_parsed(self):
        snap = _observer("mikrotik", "normal")[0].observe()
        self.assertEqual(snap.wans[0].latency_ms, 18.4)
        self.assertEqual(snap.wans[0].loss_pct, 0.0)
        self.assertEqual({w.kind for w in snap.wans}, {"ethernet", "lte"})

    def test_every_scenario_parses_for_every_vendor(self):
        for vendor in ADAPTERS:
            for sc in scenarios(vendor):
                _observer(vendor, sc)[0].observe()


class ReadOnlyTest(unittest.TestCase):
    def test_rest_transport_cannot_write(self):
        tr = SimulatedRest()
        for verb in ("put", "patch", "post", "delete", "request"):
            with self.assertRaises(ReadOnlyViolation):
                getattr(tr, verb)
        with self.assertRaises(ReadOnlyViolation):
            tr.get("system/reboot")
        with self.assertRaises(ReadOnlyViolation):
            tr.get("ip/route/add")

    def test_ubus_transport_only_allows_read_calls(self):
        tr = SimulatedUbus()
        for obj, method in (("system", "reboot"), ("network.interface.wan", "down"), ("uci", "set"),
                            ("file", "exec"), ("mwan3", "restart")):
            with self.assertRaises(ReadOnlyViolation):
                tr.call(obj, method)

    def test_adapters_only_issue_reads(self):
        obs, tr = _observer("mikrotik")
        obs.observe()
        self.assertTrue(all(c.startswith("GET ") for c in tr.calls))
        obs, tr = _observer("openwrt")
        obs.observe()
        self.assertEqual(set(tr.calls), {"ubus mwan3 status", "ubus network.interface dump"})


class ConfigTest(unittest.TestCase):
    def _load(self, routers):
        return load(overrides={"data_dir": tempfile.mkdtemp(), "routers": routers})

    def test_valid_simulated_config(self):
        cfg = self._load([{"name": "lab", "vendor": "mikrotik", "transport": "simulated", "scenario": "normal"}])
        self.assertEqual(len(cfg["routers"]), 1)

    def test_rejects_real_transport_credentials_and_unknown_vendor(self):
        bad = [
            [{"name": "x", "vendor": "mikrotik", "transport": "https"}],
            [{"name": "x", "vendor": "mikrotik", "host": "192.0.2.1"}],
            [{"name": "x", "vendor": "mikrotik", "password": "p"}],
            [{"name": "x", "vendor": "peplink"}],
            [{"name": "x", "vendor": "openwrt", "scenario": "inventado"}],
            [{"name": "x", "vendor": "openwrt", "interval_s": 1}],
            [{"name": "x", "vendor": "openwrt"}, {"name": "x", "vendor": "mikrotik"}],
        ]
        for routers in bad:
            with self.subTest(routers=routers), self.assertRaises(ConfigError):
                self._load(routers)


class WatchAndApiTest(unittest.TestCase):
    def test_changes_are_audited_once(self):
        d = tempfile.mkdtemp()
        audit = AuditLog(os.path.join(d, "a.jsonl"))
        obs, tr = _observer("openwrt")
        now = [0.0]
        rw = RouterWatch(obs, audit, 5, clock=lambda: now[0])
        rw.poll()
        rw.poll(force=True)                             # sin cambios: no se anota otra vez
        tr.set_scenario("wan1_down")
        rw.poll()                                       # dentro del intervalo: no consulta
        self.assertEqual(rw.last.active_wan, "wan")
        now[0] = 6
        rw.poll()
        evs = [e for e in audit.events(kind="router.state")]
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[-1]["data"]["active_wan"], "wanb")

    def test_read_credential_sees_routers_without_sensitive_data(self):
        eng, net = sim_engine(routers=None)
        eng.routers = build_observers([{"name": "router-lab", "vendor": "mikrotik", "scenario": "normal"}], eng.audit)
        for rw in eng.routers:
            rw.poll(force=True)
        api = CauceAPI(eng, "127.0.0.1", 0)
        api.start()
        try:
            admin = CauceClient(api.token, port=api.port)
            read = CauceReadClient(admin.create_token("monitor", "read")["token"], port=api.port)
            v = read.routers()
            self.assertEqual(v["schema"], "cauce.routers/1")
            r = v["routers"][0]
            self.assertTrue(r["simulated"])
            self.assertEqual((r["state"], r["active_wan"]), ("usable", "WAN1"))
            raw = json.dumps(v)
            for leak in ("192.0.2.", "198.51.100.", "203.0.113.", "ether1", "lte1", "*1"):
                self.assertNotIn(leak, raw)
            ev = read.events(0)["events"]
            self.assertIn("router.state", {e["kind"] for e in ev})
        finally:
            api.stop()


if __name__ == "__main__":
    unittest.main()
