import json
import os
import unittest

from helpers import FIXTURES  # noqa: F401  (ajusta sys.path)
from cauce.discovery import linux, windows
from cauce.independence import analyze
from cauce.models import Independence, Kind, LinkState


class WindowsParserTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FIXTURES, "windows_netadapter_synthetic.json"), encoding="utf-8") as fh:
            self.conns = {c.name: c for c in analyze(windows.parse(json.load(fh)))}

    def test_kinds(self):
        c = self.conns
        self.assertEqual(c["Ethernet"].kind, Kind.ETHERNET)
        self.assertEqual(c["Wi-Fi"].kind, Kind.WIFI)
        self.assertEqual(c["Celular"].kind, Kind.MOBILE)
        self.assertEqual(c["Ethernet 2"].kind, Kind.TETHER)
        self.assertEqual(c["vEthernet (WSL)"].kind, Kind.VIRTUAL)
        self.assertTrue(c["vEthernet (WSL)"].is_virtual)
        self.assertEqual(c["WireGuard Tunnel"].kind, Kind.VPN)

    def test_ip_gateway_state(self):
        c = self.conns
        self.assertEqual(c["Ethernet"].ipv4, ["192.168.1.50"])
        self.assertEqual(c["Ethernet"].gateway, "192.168.1.1")
        self.assertEqual(c["Ethernet"].route_metric, 25)
        self.assertEqual(c["Ethernet 2"].link, LinkState.DOWN)
        self.assertEqual(c["Ethernet 2"].ipv4, [], "APIPA no cuenta como dirección útil")
        self.assertIsNone(c["WireGuard Tunnel"].gateway, "NextHop 0.0.0.0 no es gateway")
        self.assertEqual(c["Ethernet 3"].gateway, "192.168.8.1", "se elige la ruta de menor métrica")

    def test_independence_two_interfaces_same_router_are_shared(self):
        c = self.conns
        self.assertEqual(c["Ethernet"].independence, Independence.SHARED)
        self.assertEqual(c["Wi-Fi"].independence, Independence.SHARED)
        self.assertEqual(c["Ethernet"].egress_group, c["Wi-Fi"].egress_group)

    def test_independence_distinct_gateway_is_only_likely(self):
        c = self.conns
        self.assertEqual(c["Celular"].independence, Independence.LIKELY)
        self.assertEqual(c["Ethernet 3"].independence, Independence.LIKELY)

    def test_same_public_ip_means_shared_even_with_distinct_gateways(self):
        c = self.conns
        c["Celular"].public_ip = "203.0.113.5"
        c["Ethernet 3"].public_ip = "203.0.113.5"
        analyze(list(c.values()))
        self.assertEqual(c["Celular"].independence, Independence.SHARED)

    def test_vm_guest_nic_is_not_virtual(self):
        # Caso real observado en la CI de GitHub (Windows Server en Azure): la NIC del
        # invitado se describe como "Microsoft Hyper-V Network Adapter" y es su única salida.
        kind, virtual = windows.classify({"Name": "Ethernet 3", "InterfaceDescription": "Microsoft Hyper-V Network Adapter #2",
                                          "Virtual": True, "HardwareInterface": False, "NdisPhysicalMedium": 14})
        self.assertEqual((kind, virtual), (Kind.ETHERNET, False))
        kind, virtual = windows.classify({"Name": "vEthernet (Default Switch)", "InterfaceDescription": "Hyper-V Virtual Ethernet Adapter"})
        self.assertEqual((kind, virtual), (Kind.VIRTUAL, True))

    def test_single_object_instead_of_array(self):
        # ConvertTo-Json convierte arrays de un elemento en objeto si no se usa @()
        payload = {"adapters": {"Name": "Ethernet", "ifIndex": 3, "Status": "Up", "NdisPhysicalMedium": 14},
                   "addresses": {"InterfaceIndex": 3, "IPAddress": "10.0.0.2", "AddressState": "Preferred"},
                   "routes": {"ifIndex": 3, "NextHop": "10.0.0.1", "RouteMetric": 0}, "ipinterfaces": None}
        cs = windows.parse(payload)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0].gateway, "10.0.0.1")
        self.assertTrue(cs[0].usable)


class LinuxParserTest(unittest.TestCase):
    def test_parse(self):
        with open(os.path.join(FIXTURES, "linux_ip_synthetic.json"), encoding="utf-8") as fh:
            d = json.load(fh)
        c = {x.name: x for x in linux.parse(d["addrs"], d["routes"])}
        self.assertEqual(c["lo"].kind, Kind.LOOPBACK)
        self.assertFalse(c["lo"].usable)
        self.assertEqual(c["enp3s0"].gateway, "192.168.1.1")
        self.assertEqual(c["wlp2s0"].link, LinkState.DOWN)
        self.assertEqual(c["wwan0"].kind, Kind.MOBILE)
        self.assertEqual(c["wwan0"].link, LinkState.UP)
        self.assertTrue(c["docker0"].is_virtual)


if __name__ == "__main__":
    unittest.main()
