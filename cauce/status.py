"""Vistas REDUCIDAS para consumidores con permiso ``read``.

Principios:
- Solo se calcula a partir del estado que el motor ya tiene en memoria. Nada de
  aquí lanza detecciones, sondeos ni lecturas del sistema (lo comprueban las pruebas).
- Lista blanca de campos: no se copia ningún diccionario del motor. Un campo nuevo
  del motor NO aparece aquí hasta añadirlo a propósito.
- No hay MAC, IP pública, IP locales, gateway, nombres de adaptador, rutas
  (leases) ni tareas de otros sistemas.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from . import API_VERSION, __version__
from .engine import Engine
from .models import Health

SCHEMA_CONNECTIVITY = "cauce.connectivity/1"
SCHEMA_EVENTS = "cauce.status-events/1"

# Tipos de evento visibles para un consumidor y los únicos campos de ``data`` que se copian.
_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "health.change": ("connection_id", "old", "new"),
    "health.recovered": ("connection_id",),
    "inventory.added": ("connection_id",),
    "inventory.changed": ("connection_id",),
    "inventory.removed": ("connection_id",),
    "engine.start": (),
    "engine.stop": (),
}
EVENT_KINDS = tuple(_EVENT_FIELDS)
MAX_EVENTS = 500


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connectivity_view(engine: Engine, now: float) -> dict[str, Any]:
    """Estado de conectividad utilizable.

    ``state``:
      usable      al menos una conexión disponible y sana
      degraded    hay conexión disponible, pero todas con latencia alta o fallos parciales
      unavailable hay mediciones y ninguna conexión está disponible
      unknown     todavía no hay mediciones suficientes (ver ``reason``); ``usable`` es null
    ``usable`` es true/false solo cuando el motor tiene base para decirlo; si no, null.
    Este documento solo existe si el motor responde: si no responde, el consumidor
    debe concluir "sin información", NUNCA "sin internet" (ver docs/LECTURA_PROTEGIDA.md).
    """
    snap = engine.health.snapshot()
    conns = []
    for c in sorted(engine.connections(), key=lambda c: c.id):
        h = snap.get(c.id, {})
        state = h.get("state", Health.UNKNOWN.value)
        measured = bool(h.get("probes", 0)) and state != Health.UNKNOWN.value
        conns.append({
            "id": c.id,
            "kind": c.kind.value,
            "link": c.link.value,
            "available": engine.routes.is_available(c.id),
            "measured": measured,
            "health": state,
            "availability_pct": h.get("availability_pct"),
            "avg_latency_ms": h.get("avg_latency_ms"),
        })
    candidates = [x for x in conns if x["link"] == "up"]
    healthy = [x for x in conns if x["available"] and x["health"] == Health.HEALTHY.value]
    degraded = [x for x in conns if x["available"] and x["health"] == Health.DEGRADED.value]
    pending = [x for x in candidates if not x["measured"]]
    reason = None
    if healthy:
        state, usable = "usable", True
    elif degraded:
        state, usable = "degraded", True
    elif pending:
        state, usable, reason = "unknown", None, "insufficient_measurements"
    else:
        state, usable = "unavailable", False
        reason = "no_active_links" if not candidates else "all_measured_connections_down"
    return {
        "schema": SCHEMA_CONNECTIVITY,
        "api": API_VERSION,
        "engine": {"version": __version__, "mode": engine.mode, "epoch": engine.audit.epoch},
        "generated_at": _iso(now),
        "last_seq": engine.audit.last_seq,
        "state": state,
        "usable": usable,
        "reason": reason,
        "counts": {"connections": len(conns), "available": sum(1 for x in conns if x["available"]),
                   "measured": sum(1 for x in conns if x["measured"])},
        "connections": conns,
    }


def _sanitize(ev: dict[str, Any]) -> dict[str, Any]:
    allowed = _EVENT_FIELDS.get(ev["kind"], ())
    out = {"seq": ev["seq"], "ts": _iso(ev["ts"]), "kind": ev["kind"]}
    for k in allowed:
        out[k] = ev["data"].get(k)
    return out


def events_view(engine: Engine, since: int, limit: int) -> dict[str, Any]:
    """Eventos reducidos en orden cronológico, con detección explícita de huecos.

    ``resync_required`` es true si el consumidor debe volver a pedir ``/status/connectivity``:
    hueco (se descartaron eventos), reinicio del motor (``since`` mayor que ``last_seq``).
    ``has_more`` indica que hay más eventos posteriores a ``next_since``.
    Nota: ``seq`` es global y cuenta también eventos no visibles aquí, así que los saltos
    de ``seq`` entre eventos devueltos son normales; el hueco real lo indica ``gap``.
    """
    last = engine.audit.last_seq
    evs, more, gap = engine.audit.events_page(since, EVENT_KINDS, limit)
    restarted = since > last
    out = [_sanitize(e) for e in evs]
    if out:
        next_since = out[-1]["seq"] if more else max(last, out[-1]["seq"])
    else:
        next_since = last
    return {
        "schema": SCHEMA_EVENTS,
        "api": API_VERSION,
        "epoch": engine.audit.epoch,
        "last_seq": last,
        "gap": gap,
        "restarted": restarted,
        "resync_required": gap or restarted,
        "has_more": more,
        "next_since": next_since,
        "events": out,
    }
