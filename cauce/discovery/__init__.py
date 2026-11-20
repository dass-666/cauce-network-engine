"""Detección de interfaces de red.

Cada proveedor devuelve una lista de ``Connection``. La lógica de análisis
(parseo) está separada de la ejecución del comando para poder probarla con
salidas guardadas (fixtures) sin necesidad de Windows.
"""
from __future__ import annotations

import sys
from typing import Protocol

from ..models import Connection


class DiscoveryProvider(Protocol):
    name: str

    def discover(self) -> list[Connection]: ...


def default_provider() -> DiscoveryProvider:
    if sys.platform == "win32":
        from .windows import WindowsDiscovery
        return WindowsDiscovery()
    if sys.platform.startswith("linux"):
        from .linux import LinuxDiscovery
        return LinuxDiscovery()
    raise RuntimeError(f"Plataforma no soportada para detección real: {sys.platform}")
