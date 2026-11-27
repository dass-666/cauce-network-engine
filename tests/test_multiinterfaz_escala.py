"""E5: cobertura multiinterfaz SIMULADA (1, 2, 5, 10 y 20 interfaces y casos difíciles).

Distingue en cada conexión: detectada / enlace activo / conectividad verificada / salida distinta / independencia de
proveedor (nunca verificada por el motor) y, a nivel global, disponibilidad del servicio (no medida) y continuidad de
sesión. Nada aquí cambia rutas del sistema: todo es el simulador. La rotación sigue apagada fuera del laboratorio.
"""
import time
import unittest

from helpers import sim_engine
from cauce.models import Kind
from cauce.simulator import SimLink
from cauce.status import connectivity_view

KINDS = [Kind.ETHERNET, Kind.WIFI, Kind.MOBILE, Kind.TETHER]


def enlaces(n, compartida_cada=None):
    out = []
    for i in range(n):
        pub = f"198.51.100.{(i // compartida_cada) + 1}" if compartida_cada else f"198.51.100.{i + 1}"
        out.append(SimLink(f"if-{i:02d}", f"Interfaz {i}", KINDS[i % 4], f"10.{i + 1}.0.2", f"10.{i + 1}.0.1", pub,
                           10 + i, "up", 15.0 + i, mac=f"02-00-00-00-00-{i:02X}"))
    return out


def vista(eng, now=None):
    return connectivity_view(eng, now or time.time())


def medir(eng, veces=4):
    for _ in range(veces):
        eng.health.check_all(force=True)


class EscalaTest(unittest.TestCase):
    def test_1_2_5_10_20_interfaces(self):
        for n in (1, 2, 5, 10, 20):
            with self.subTest(n=n):
                eng, net = sim_engine(links=enlaces(n), sessions={"max": 8})
                t0 = time.monotonic()
                medir(eng, 1)
                ronda = time.monotonic() - t0
                v = vista(eng)
                self.assertEqual(v["counts"]["connections"], n)
                self.assertEqual({c["status"] for c in v["connections"]}, {"ok"})
                self.assertEqual(v["state"], "usable")
                self.assertTrue(all(c["evidence"]["connectivity_verified"] for c in v["connections"]))
                self.assertTrue(all(c["evidence"]["provider_independence"] == "not_verified" for c in v["connections"]))
                self.assertLess(ronda, 5.0, f"una ronda de sondeo con {n} interfaces tardó {ronda:.2f} s")
                self.assertFalse(eng.cfg["lab"]["rotation_enabled"])

    def test_20_caidas_simultaneas_y_recuperacion(self):
        eng, net = sim_engine(links=enlaces(20))
        for i in range(20):
            net.set_state(f"if-{i:02d}", "down")
        medir(eng)
        v = vista(eng)
        self.assertEqual((v["state"], v["usable"], v["reason"]), ("unavailable", False, "all_measured_connections_down"))
        self.assertTrue(all(c["evidence"]["link_active"] and not c["evidence"]["connectivity_verified"]
                            for c in v["connections"]), "enlace activo sin internet: detectado, no verificado")
        for i in range(0, 20, 2):                    # vuelve la mitad
            net.set_state(f"if-{i:02d}", "up")
        medir(eng)
        st = {c["id"]: c["status"] for c in vista(eng)["connections"]}
        self.assertEqual(sum(1 for x in st.values() if x == "ok"), 10)
        self.assertEqual(vista(eng)["state"], "usable")

    def test_aislamiento_entre_interfaces(self):
        eng, net = sim_engine(links=enlaces(10))
        net.set_state("if-03", "down")
        net.links["if-07"].latency_ms = 900.0
        medir(eng)
        st = {c["id"]: c["status"] for c in vista(eng)["connections"]}
        self.assertEqual((st["if-03"], st["if-07"]), ("down", "degraded"))
        self.assertEqual(sum(1 for k, x in st.items() if x == "ok"), 8, "un fallo no contamina a las demás")


class CasosDificilesTest(unittest.TestCase):
    def test_salida_duplicada_no_cuenta_como_independiente(self):
        eng, net = sim_engine(links=enlaces(6, compartida_cada=2))     # pares con la misma IP pública
        eg = {c["id"]: c["evidence"]["egress"] for c in vista(eng)["connections"]}
        self.assertEqual(set(eg.values()), {"shared"})

    def test_vpn_y_virtuales_se_marcan_y_no_dan_independencia(self):
        ls = enlaces(2) + [
            SimLink("vpn-1", "Tailscale", Kind.VPN, "100.64.0.2", "100.64.0.1", "198.51.100.1", 5, "up", 30.0),
            SimLink("vir-1", "Hyper-V", Kind.VIRTUAL, "172.16.0.2", "172.16.0.1", None, 100, "up", 1.0),
        ]
        eng, net = sim_engine(links=ls)
        ev = {c["id"]: c["evidence"] for c in vista(eng)["connections"]}
        self.assertTrue(ev["vpn-1"]["virtual_or_vpn"] and ev["vir-1"]["virtual_or_vpn"])
        self.assertEqual(ev["vpn-1"]["egress"], "shared", "la VPN sale por la misma IP pública que if-00")
        self.assertNotIn(ev["vir-1"]["egress"], ("distinct_egress_address",))
        self.assertFalse(ev["if-00"]["virtual_or_vpn"])

    def test_adaptadores_usb_y_telefonos_en_caliente_varios_a_la_vez(self):
        eng, net = sim_engine(links=enlaces(2))
        for i in range(4):
            net.plug(SimLink(f"usb-{i}", f"USB {i}", Kind.TETHER if i % 2 else Kind.ETHERNET, f"192.168.{40 + i}.10",
                             f"192.168.{40 + i}.1", f"203.0.113.{i + 1}", 60 + i, "up", 50.0, mac=f"02-AA-00-00-00-0{i}"))
        eng.refresh()
        medir(eng, 1)
        self.assertEqual(vista(eng)["counts"]["connections"], 6)
        for i in range(4):
            net.unplug(f"usb-{i}")
        eng.refresh()
        self.assertEqual(vista(eng)["counts"]["connections"], 2)

    def test_cambio_de_identificador_de_varias_interfaces(self):
        eng, net = sim_engine(links=enlaces(3))
        for i in range(3):
            l = net.links.pop(f"if-{i:02d}")
            l.id = f"nuevo-{i}"
            net.plug(l)
        eng.refresh()
        re = [(e["data"]["previous_id"], e["data"]["connection_id"]) for e in eng.audit.events(limit=1000)
              if e["kind"] == "inventory.reidentified"]
        self.assertEqual(sorted(re), [(f"if-{i:02d}", f"nuevo-{i}") for i in range(3)])

    def test_suspension_con_20_interfaces_invalida_y_vuelve_a_medir(self):
        eng, net = sim_engine(links=enlaces(20))
        later = time.time() + eng.health.stale_after_s() + 60      # el equipo durmió: ninguna medición sirve
        v = vista(eng, later)
        self.assertEqual({c["status"] for c in v["connections"]}, {"unverified"})
        self.assertEqual((v["state"], v["usable"]), ("unknown", None))
        self.assertTrue(eng.check_resume(time.time() + 5000, time.time()))
        self.assertEqual({c["status"] for c in vista(eng)["connections"]}, {"ok"})

    def test_continuidad_de_sesion_separada_de_la_red(self):
        eng, net = sim_engine(links=enlaces(2))
        v = vista(eng)["evidence"]
        self.assertEqual((v["service_availability"], v["session_continuity"]["sessions"]),
                         ("not_measured_by_engine", 0))
        eng.sessions.register("t1", "forms", {"label": "forms-a", "authorized_by_user": True, "connection_id": "if-00"})
        self.assertEqual(vista(eng)["evidence"]["session_continuity"]["uncertain"], 1,
                         "registrada sin latido: incierta, aunque la red esté bien")


if __name__ == "__main__":
    unittest.main()
