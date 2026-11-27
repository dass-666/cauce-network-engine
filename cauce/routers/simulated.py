"""Transportes SIMULADOS para los adaptadores de routers.

Las respuestas imitan el FORMATO documentado de cada fabricante, pero **no son capturas de un equipo
real** (no tenemos el hardware). Antes de usar un adaptador con un router de verdad hay que guardar una
captura real en ``tests/fixtures/routers/`` y comprobar que el adaptador la interpreta igual.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from .contract import ReadOnlyViolation, RouterUnavailable

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# Únicas llamadas ubus que un adaptador de OpenWrt puede hacer (todas de lectura).
UBUS_READ_ALLOWLIST = {("mwan3", "status"), ("network.interface", "dump")}
# Únicos recursos REST que un adaptador de MikroTik puede leer.
REST_READ_ALLOWLIST = {"interface", "ip/route", "tool/netwatch"}


def load_fixture(vendor: str) -> dict[str, Any]:
    with open(os.path.join(FIXTURES_DIR, f"{vendor}.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def scenarios(vendor: str) -> list[str]:
    return sorted(load_fixture(vendor)["scenarios"])


class _Simulated:
    vendor = ""

    def __init__(self, scenario: str = "normal"):
        self._data = load_fixture(self.vendor)
        self.calls: list[str] = []          # registro de lo que pidió el adaptador (para las pruebas)
        self.offline = False                # simula que el router no responde
        self.set_scenario(scenario)

    def set_scenario(self, scenario: str) -> None:
        if scenario not in self._data["scenarios"]:
            raise ValueError(f"Escenario desconocido para {self.vendor}: {scenario}")
        self.scenario = scenario

    def _lookup(self, key: str) -> Any:
        if self.offline:
            raise RouterUnavailable("router simulado sin respuesta")
        sc = self._data["scenarios"][self.scenario]
        if key not in sc:
            raise RouterUnavailable(f"sin respuesta simulada para {key}")
        return copy.deepcopy(sc[key])


class SimulatedRest(_Simulated):
    """Imita ``GET /rest/<path>`` de RouterOS v7 (los valores llegan como texto, igual que en RouterOS)."""
    vendor = "mikrotik"

    def get(self, path: str) -> Any:
        path = path.strip("/")
        if path not in REST_READ_ALLOWLIST:
            raise ReadOnlyViolation(f"recurso no permitido para un observador: {path}")
        self.calls.append("GET " + path)
        return self._lookup(path)

    # Cualquier intento de escribir falla de forma explícita (y la prueba lo comprueba).
    def __getattr__(self, name: str):
        if name in ("put", "patch", "post", "delete", "request"):
            raise ReadOnlyViolation(f"el transporte de observación no permite {name.upper()}")
        raise AttributeError(name)


class SimulatedUbus(_Simulated):
    """Imita ``ubus call <obj> <method>`` de OpenWrt vía JSON-RPC (uhttpd-mod-ubus/rpcd)."""
    vendor = "openwrt"

    def call(self, obj: str, method: str) -> Any:
        if (obj, method) not in UBUS_READ_ALLOWLIST:
            raise ReadOnlyViolation(f"llamada ubus no permitida para un observador: {obj} {method}")
        self.calls.append(f"ubus {obj} {method}")
        return self._lookup(f"{obj} {method}")
