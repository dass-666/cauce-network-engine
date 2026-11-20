"""Auditoría: registro de eventos append-only con encadenado de hashes.

Cada evento incluye el hash SHA-256 del evento anterior. Si alguien edita o
borra una línea del archivo, ``verify_file`` detecta la ruptura de la cadena.
Esto no impide la manipulación (quien tenga acceso al archivo puede reescribir
toda la cadena), pero sí la hace detectable frente a ediciones parciales.

Los campos cuyo nombre sugiere un secreto se redactan antes de escribirse.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable, Optional

_SECRET_HINTS = ("password", "passwd", "secret", "token", "credential", "apikey", "api_key", "psk")
GENESIS = "0" * 64


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(h in str(k).lower() for h in _SECRET_HINTS):
                out[k] = "[REDACTADO]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _digest(prev_hash: str, body: dict[str, Any]) -> str:
    raw = prev_hash + json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Optional[str] = None, memory_size: int = 2000):
        self._lock = threading.Lock()
        self._path = path
        self._events: deque[dict[str, Any]] = deque(maxlen=memory_size)
        self._seq = 0
        self._last_hash = GENESIS
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self.epoch = os.urandom(4).hex()   # cambia en cada arranque: permite a un consumidor detectar reinicios
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._resume_from_file(path)

    def _resume_from_file(self, path: str) -> None:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._seq = max(self._seq, int(ev.get("seq", 0)))
                self._last_hash = ev.get("hash", self._last_hash)

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def record(self, kind: str, message: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            body = {
                "seq": self._seq,
                "ts": round(time.time(), 3),
                "kind": kind,
                "message": message,
                "data": redact(data),
            }
            h = _digest(self._last_hash, body)
            ev = dict(body, prev=self._last_hash, hash=h)
            self._last_hash = h
            self._events.append(ev)
            if self._path:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(ev)
            except Exception:  # un suscriptor defectuoso no debe romper el motor
                pass
        return ev

    def events(self, since: int = 0, kind: Optional[str] = None, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            evs = [e for e in self._events if e["seq"] > since and (kind is None or e["kind"].startswith(kind))]
        return evs[-limit:]

    def events_page(self, since: int, kinds: tuple[str, ...], limit: int) -> tuple[list[dict[str, Any]], bool, bool]:
        """Lectura para consumidores: los PRIMEROS ``limit`` eventos con seq > since (orden cronológico).

        Devuelve (eventos, hay_mas, hueco). ``hueco`` es True si se descartaron eventos entre ``since`` y el
        más antiguo que aún se conserva en memoria; en ese caso el consumidor debe releer el estado completo.
        """
        with self._lock:
            oldest = self._events[0]["seq"] if self._events else self._seq + 1
            gap = since + 1 < oldest and since < self._seq
            evs = [e for e in self._events if e["seq"] > since and e["kind"].startswith(kinds)]
        return evs[:limit], len(evs) > limit, gap

    @property
    def last_seq(self) -> int:
        return self._seq


def verify_chain(events: Iterable[dict[str, Any]]) -> tuple[bool, Optional[int]]:
    """Devuelve (ok, seq_del_primer_evento_roto)."""
    prev = None
    for ev in events:
        body = {k: ev[k] for k in ("seq", "ts", "kind", "message", "data")}
        if prev is not None and ev.get("prev") != prev:
            return False, ev.get("seq")
        if _digest(ev.get("prev", GENESIS), body) != ev.get("hash"):
            return False, ev.get("seq")
        prev = ev["hash"]
    return True, None


def verify_file(path: str) -> tuple[bool, Optional[int]]:
    with open(path, "r", encoding="utf-8") as fh:
        evs = [json.loads(line) for line in fh if line.strip()]
    return verify_chain(evs)
