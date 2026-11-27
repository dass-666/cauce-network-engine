"""E4: supervisión de N interfaces, teléfono por USB en caliente, cambio de identificador, suspensión y veredictos."""
import time
import unittest

from helpers import sim_engine
from cauce.discovery.windows import classify
from cauce.models import Health, Kind
from cauce.simulator import SimLink, three_links
from cauce.status import connectivity_view


def telefono(cid="sim-p", mac="02-11-22-33-44-55", state="up"):
    return SimLink(cid, "Samsung por USB", Kind.TETHER, "192.168.42.10", "192.168.42.129", "203.0.113.77", 70,
                   state, 60.0, mac=mac)


def eventos(eng, kind):
    return [dict(e["data"], message=e["message"]) for e in eng.audit.events(limit=10000) if e["kind"] == kind]


class ClasificacionTest(unittest.TestCase):
    def test_telefonos_y_adaptadores_usb(self):
        casos = {
            "Remote NDIS based Internet Sharing Device": Kind.TETHER,
            "SAMSUNG Mobile USB Remote NDIS Network Device": Kind.TETHER,
            "UsbNcm Host Device": Kind.TETHER,                      # Android reciente por USB-C (NCM)
            "ASIX AX88179 USB 3.0 to Gigabit Ethernet Adapter": Kind.ETHERNET,
            "Realtek USB GbE Family Controller": Kind.ETHERNET,
            "Tailscale Tunnel": Kind.VPN,
            "Hyper-V Virtual Ethernet Adapter": Kind.VIRTUAL,
        }
        for desc, kind in casos.items():
            ad = {"Name": "Ethernet 3", "InterfaceDescription": desc, "NdisPhysicalMedium": 14,
                  "Virtual": False, "HardwareInterface": True}
            if kind == Kind.VIRTUAL:
                ad.update(Virtual=True, HardwareInterface=False)
            self.assertEqual(classify(ad)[0], kind, desc)
        self.assertFalse(classify({"InterfaceDescription": "UsbNcm Host Device", "Virtual": False})[1],
                         "un teléfono es una salida física, no un adaptador virtual")


class EnCalienteTest(unittest.TestCase):
    def test_conectar_y_desconectar_telefono(self):
        eng, net = sim_engine()
        net.plug(telefono())
        eng.refresh()
        eng.health.check_all(force=True)
        self.assertEqual(eng.health.state_of("sim-p"), Health.HEALTHY)
        self.assertTrue(any(e.get("connection_id") == "sim-p" for e in eventos(eng, "inventory.added")))
        lease = eng.routes.acquire("prueba", "t", "sim-p", allow_failover=True, allow_failback=True)
        net.unplug("sim-p")
        eng.refresh()
        self.assertTrue(any(e.get("connection_id") == "sim-p" for e in eventos(eng, "inventory.removed")))
        self.assertNotEqual(lease.connection_id, "sim-p", "la tarea no se queda en un adaptador que ya no existe")
        self.assertNotIn("sim-p", eng.health.snapshot(), "su salud no queda «sana» para siempre")

    def test_mismo_telefono_con_otro_identificador(self):
        eng, net = sim_engine()
        net.plug(telefono("win-21"))
        eng.refresh()
        net.unplug("win-21")
        net.plug(telefono("win-34"))                 # otro puerto USB: Windows le da otro ifIndex
        eng.refresh()
        re = eventos(eng, "inventory.reidentified")
        self.assertEqual([(e["previous_id"], e["connection_id"]) for e in re], [("win-21", "win-34")])
        self.assertFalse(any(e.get("connection_id") == "win-21" for e in eventos(eng, "inventory.removed")))

    def test_mac_vacia_o_nula_no_se_confunde(self):
        eng, net = sim_engine()
        net.plug(telefono("a", mac="00-00-00-00-00-00"))
        eng.refresh()
        net.unplug("a")
        net.plug(telefono("b", mac="00-00-00-00-00-00"))
        eng.refresh()
        self.assertEqual(eventos(eng, "inventory.reidentified"), [])

    def test_enlace_activo_sin_internet_es_caido_no_desconectado(self):
        eng, net = sim_engine()
        net.plug(telefono(state="down"))             # cable puesto, sin datos móviles
        eng.refresh()
        for _ in range(4):
            eng.health.check_all(force=True)
        v = {c["id"]: c for c in connectivity_view(eng, time.time())["connections"]}
        self.assertEqual((v["sim-p"]["link"], v["sim-p"]["status"]), ("up", "down"))


class VariasInterfacesTest(unittest.TestCase):
    def test_cinco_enlaces_se_supervisan_todos(self):
        links = three_links() + [telefono(), SimLink("sim-e", "USB-C Ethernet", Kind.ETHERNET, "10.50.0.2",
                                                    "10.50.0.1", "198.51.100.50", 20, "up", 15.0)]
        links[2].state = "up"
        eng, net = sim_engine(links=links)
        v = connectivity_view(eng, time.time())
        self.assertEqual(len(v["connections"]), 5)
        self.assertEqual({c["status"] for c in v["connections"]}, {"ok"})
        net.set_state("sim-a", "down")
        net.set_state("sim-p", "down")
        for _ in range(4):
            eng.health.check_all(force=True)
        v = {c["id"]: c["status"] for c in connectivity_view(eng, time.time())["connections"]}
        self.assertEqual(v, {"sim-a": "down", "sim-b": "ok", "sim-c": "ok", "sim-p": "down", "sim-e": "ok"})


class VeredictoTest(unittest.TestCase):
    def test_cuatro_veredictos(self):
        links = three_links()
        links[1].latency_ms = 900.0                  # LTE lenta -> degradada
        eng, net = sim_engine(links=links)
        v = {c["id"]: c["status"] for c in connectivity_view(eng, time.time())["connections"]}
        self.assertEqual(v, {"sim-a": "ok", "sim-b": "degraded", "sim-c": "down"})
        later = time.time() + eng.health.stale_after_s() + 1
        v = {c["id"]: c["status"] for c in connectivity_view(eng, later)["connections"]}
        self.assertEqual(v["sim-a"], "unverified", "una medición vieja nunca se da por buena")


class SuspensionTest(unittest.TestCase):
    def test_reanudar_vuelve_a_medir_y_lo_anota(self):
        eng, net = sim_engine()
        self.assertFalse(eng.check_resume(1000.0, 995.0))
        probes = eng.health.snapshot()["sim-a"]["probes"]
        self.assertTrue(eng.check_resume(5000.0, 1000.0))
        self.assertEqual(len(eventos(eng, "system.resume_detected")), 1)
        self.assertGreater(eng.health.snapshot()["sim-a"]["probes"], probes, "se sondea de inmediato")


if __name__ == "__main__":
    unittest.main()
