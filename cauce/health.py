"""Supervisión de salud por conexión.

- Sondeos TCP a destinos de diagnóstico AUTORIZADOS (lista en la configuración),
  siempre enlazados a la IP de la conexión supervisada.
- Histéresis: hacen falta ``fail_threshold`` fallos seguidos para declarar
  DOWN y ``recover_threshold`` éxitos seguidos para volver a HEALTHY. Un único
  error no provoca conmutación (evita oscilaciones).
- Sin ciclos infinitos ni sondeo agresivo: cuando una conexión está DOWN, el
  intervalo crece exponencialmente hasta ``max_interval_s``.
- 0.2.0: cuando una conexión DOWN responde bien una vez, el siguiente sondeo de confirmación se hace al
  intervalo normal (no con el retroceso acumulado). La histéresis no cambia (siguen haciendo falta
  ``recover_threshold`` éxitos), pero la recuperación ya no tarda dos esperas largas sino como mucho una.
- 0.2.0: cada conexión informa cuándo se sondeó por última vez. Si el último sondeo es más viejo que
  ``stale_after_s()`` (p. ej. porque el hilo de salud se atascó), su estado se marca como NO VERIFICADO
  para que nadie muestre como sana una conexión que ya no se está midiendo.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from .audit import AuditLog
from .models import Connection, Health, ProbeResult
from .net import Network


@dataclass
class HealthConfig:
    targets: list[str] = field(default_factory=list)   # "host:puerto"
    interval_s: float = 5.0
    max_interval_s: float = 60.0
    timeout_s: float = 3.0
    fail_threshold: int = 3
    recover_threshold: int = 2
    degraded_latency_ms: float = 800.0
    window: int = 50


@dataclass
class ConnHealth:
    state: Health = Health.UNKNOWN
    consecutive_fail: int = 0
    consecutive_ok: int = 0
    probes: int = 0
    failures: int = 0
    last_latency_ms: Optional[float] = None
    last_error: Optional[str] = None
    last_change: float = field(default_factory=time.time)
    next_due: float = 0.0
    backoff_level: int = 0
    last_probe: Optional[float] = None
    samples: deque = field(default_factory=lambda: deque(maxlen=50))

    def to_dict(self) -> dict:
        lat = [s.latency_ms for s in self.samples if s.ok and s.latency_ms is not None]
        oks = sum(1 for s in self.samples if s.ok)
        return {
            "state": self.state.value,
            "consecutive_fail": self.consecutive_fail,
            "consecutive_ok": self.consecutive_ok,
            "probes": self.probes,
            "failures": self.failures,
            "availability_pct": round(100.0 * oks / len(self.samples), 1) if self.samples else None,
            "avg_latency_ms": round(sum(lat) / len(lat), 2) if lat else None,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "last_change": self.last_change,
            "backoff_level": self.backoff_level,
            "last_probe": self.last_probe,
            "next_due": self.next_due,
        }


Listener = Callable[[str, Health, Health], None]


class HealthMonitor:
    def __init__(self, network: Network, audit: AuditLog, cfg: HealthConfig,
                 get_connections: Callable[[], list[Connection]], clock: Callable[[], float] = time.time):
        self.network = network
        self.audit = audit
        self.cfg = cfg
        self.get_connections = get_connections
        self.clock = clock
        self._state: dict[str, ConnHealth] = {}
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def stale_after_s(self) -> float:
        """Antigüedad máxima del último sondeo para que su resultado siga valiendo: dos esperas máximas más los
        tiempos de espera de todos los destinos (un ciclo lento pero vivo nunca llega a esto)."""
        return 2 * self.cfg.max_interval_s + self.cfg.timeout_s * max(1, len(self.cfg.targets)) + 5

    def on_change(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def state_of(self, conn_id: str) -> Health:
        with self._lock:
            h = self._state.get(conn_id)
            return h.state if h else Health.UNKNOWN

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {k: v.to_dict() for k, v in self._state.items()}

    def _apply(self, conn: Connection, results: list[ProbeResult]) -> None:
        now = self.clock()
        transitions = []
        with self._lock:
            h = self._state.setdefault(conn.id, ConnHealth(samples=deque(maxlen=self.cfg.window)))
            ok_results = [r for r in results if r.ok]
            ok = bool(ok_results)  # basta un destino sano: un destino caído no es la conexión caída
            for r in results:
                h.samples.append(r)
            h.probes += 1
            h.last_probe = now
            if ok:
                h.consecutive_ok += 1
                h.consecutive_fail = 0
                h.last_latency_ms = min(r.latency_ms for r in ok_results if r.latency_ms is not None) if any(r.latency_ms is not None for r in ok_results) else None
                h.last_error = None if len(ok_results) == len(results) else next(r.error for r in results if not r.ok)
            else:
                h.consecutive_fail += 1
                h.consecutive_ok = 0
                h.failures += 1
                h.last_error = results[0].error if results else "sin destinos de diagnóstico configurados"
            old = h.state
            if ok:
                if old == Health.DOWN and h.consecutive_ok < self.cfg.recover_threshold:
                    new = old  # recuperación aún no confirmada
                else:
                    degraded = (h.last_latency_ms or 0) > self.cfg.degraded_latency_ms or len(ok_results) < len(results)
                    new = Health.DEGRADED if degraded else Health.HEALTHY
            else:
                if h.consecutive_fail >= self.cfg.fail_threshold:
                    new = Health.DOWN
                elif old == Health.HEALTHY:
                    new = Health.DEGRADED
                else:
                    new = old
            # Programación del siguiente sondeo con retroceso exponencial
            if new == Health.DOWN and ok:
                # Recuperación en curso: confirmar pronto, sin acumular más retroceso.
                delay = self.cfg.interval_s
            elif new == Health.DOWN:
                h.backoff_level = min(h.backoff_level + 1, 10)
                delay = min(self.cfg.interval_s * (2 ** (h.backoff_level - 1)), self.cfg.max_interval_s)
            else:
                h.backoff_level = 0
                delay = self.cfg.interval_s
            h.next_due = now + delay
            if new != old:
                h.state = new
                h.last_change = now
                transitions.append((old, new, h.last_error, h.last_latency_ms))
        for old, new, err, lat in transitions:
            self.audit.record("health.change", f"{conn.name}: {old.value} -> {new.value}",
                              connection_id=conn.id, old=old.value, new=new.value, error=err, latency_ms=lat)
            for fn in list(self._listeners):
                fn(conn.id, old, new)

    def check(self, conn: Connection) -> Health:
        """Sondeo inmediato y síncrono de una conexión."""
        if not conn.usable:
            results = [ProbeResult(conn.id, "-", False, None, f"enlace {conn.link.value} o sin IPv4", None)]
        elif not self.cfg.targets:
            results = []
        else:
            results = [self.network.probe(conn, t, self.cfg.timeout_s) for t in self.cfg.targets]
        if not results and conn.usable:
            # Sin destinos autorizados no se sondea: la salud queda desconocida (no se inventa)
            return self.state_of(conn.id)
        self._apply(conn, results)
        return self.state_of(conn.id)

    def confirm_down(self, conn: Connection) -> bool:
        """Ante un error de una tarea: sondea hasta ``fail_threshold`` veces.
        True solo si la conexión queda declarada DOWN (pérdida auténtica)."""
        for _ in range(max(1, self.cfg.fail_threshold)):
            st = self.check(conn)
            if st == Health.DOWN:
                return True
            with self._lock:
                h = self._state.get(conn.id)
                if h and h.consecutive_ok > 0:
                    return False
            if not self.cfg.targets and conn.usable:
                return False
        return self.state_of(conn.id) == Health.DOWN

    def check_all(self, force: bool = False) -> None:
        now = self.clock()
        for c in self.get_connections():
            with self._lock:
                h = self._state.get(c.id)
                due = h is None or force or now >= h.next_due
            if due:
                self.check(c)

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cauce-health", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception as e:  # registrar y seguir; nunca matar el hilo en silencio
                self.audit.record("error", f"fallo en ciclo de salud: {e!r}")
            self._stop.wait(min(1.0, self.cfg.interval_s))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
