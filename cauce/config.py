"""Configuración: JSON simple con valores por defecto seguros y validación."""
from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Optional

from . import DEFAULT_API_PORT
from .audit import _SECRET_HINTS

# Variable de entorno que fija el puerto de la API sin tocar el archivo de configuración.
PORT_ENV = "CAUCE_NE_PORT"

LOOPBACK_HOSTS = {"127.0.0.1"}

DEFAULTS: dict[str, Any] = {
    "api": {"host": "127.0.0.1", "port": DEFAULT_API_PORT},
    "health": {
        # Destinos de diagnóstico autorizados ("host:puerto"). Solo conexión TCP,
        # sin enviar datos. Cámbialos por destinos que tengas permiso de sondear.
        "targets": ["1.1.1.1:443", "9.9.9.9:443"],
        "interval_s": 5.0, "max_interval_s": 60.0, "timeout_s": 3.0,
        "fail_threshold": 3, "recover_threshold": 2, "degraded_latency_ms": 800.0,
    },
    "lab": {
        # Únicos destinos permitidos para tareas de laboratorio y rotación.
        "allowed_hosts": ["127.0.0.1", "localhost"],
        # Rotación de leases entre conexiones. Con el motor REAL está APAGADA salvo que se active aquí a
        # propósito para un laboratorio propio. Con el simulador siempre está disponible (no hay red real).
        # Nunca se usa contra servicios de terceros: un 429/403 detiene la operación igualmente.
        "rotation_enabled": False,
    },
    # Observación de routers (solo lectura). En esta versión solo hay transportes simulados.
    "routers": [],
    "discovery": {
        "refresh_s": 30.0,
        "include_virtual": False,
        "exclude": [],          # nombres o ids de conexiones que el motor NUNCA usa
        "only": [],             # si no está vacío, solo se gestionan estas
    },
    "egress_check_url": None,   # URL propia que devuelve la IP pública (opcional)
    "data_dir": None,
}


class ConfigError(Exception):
    pass


def default_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "CauceNetworkEngine")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "cauce-network-engine")


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _find_secrets(obj: Any, path: str = "") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if any(h in str(k).lower() for h in _SECRET_HINTS):
                found.append(p)
            found += _find_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _find_secrets(v, f"{path}[{i}]")
    return found


def validate(cfg: dict) -> dict:
    secrets_found = _find_secrets(cfg)
    if secrets_found:
        raise ConfigError("La configuración no puede contener credenciales en texto plano: " + ", ".join(secrets_found))
    host = cfg["api"]["host"]
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(f"La API solo puede escuchar en 127.0.0.1 (se pidió {host!r})")
    port = int(cfg["api"]["port"])
    if not (1024 <= port <= 65535):
        raise ConfigError("Puerto de API fuera de rango (1024-65535)")
    for t in cfg["health"]["targets"]:
        if ":" not in t:
            raise ConfigError(f"Destino de diagnóstico inválido {t!r}; formato host:puerto")
    if not cfg["lab"]["allowed_hosts"]:
        raise ConfigError("lab.allowed_hosts no puede estar vacío")
    if not isinstance(cfg["lab"].get("rotation_enabled", False), bool):
        raise ConfigError("lab.rotation_enabled debe ser true o false")
    from .routers import validate_router_config
    validate_router_config(cfg.get("routers") or [])
    cfg["data_dir"] = cfg.get("data_dir") or default_data_dir()
    return cfg


def load(path: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = _merge(cfg, json.load(fh))
    env_port = os.environ.get(PORT_ENV)
    if env_port:
        try:
            cfg = _merge(cfg, {"api": {"port": int(env_port)}})
        except ValueError:
            raise ConfigError(f"{PORT_ENV} debe ser un número de puerto (se recibió {env_port!r})")
    if overrides:
        cfg = _merge(cfg, overrides)
    return validate(cfg)
