"""Orquestador: une detección, salud, rutas, rotación, ejecutor y auditoría."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

from . import __version__
from .audit import AuditLog
from .executor import Step, TaskExecutor
from .health import HealthConfig, HealthMonitor
from .independence import analyze
from .models import Connection, Health, TERMINAL_TASK_STATES
from .net import Network
from .policy import RotationController
from .routing import RouteError, RouteManager
from .sessions import SessionRegistry


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Engine:
    def __init__(self, cfg: dict, discovery, network: Network, audit: Optional[AuditLog] = None,
                 mode: str = "real"):
        self.cfg = cfg
        self.mode = mode
        self.discovery = discovery
        self.network = network
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.audit = audit or AuditLog(os.path.join(cfg["data_dir"], "audit.jsonl"))
        self._conns: dict[str, Connection] = {}
        self._conn_lock = threading.RLock()
        self.started_at = time.time()
        self.last_discovery_error: Optional[str] = None
        self._egress_cache: dict = {}
        hc = cfg["health"]
        self.health = HealthMonitor(network, self.audit, HealthConfig(
            targets=list(hc["targets"]), interval_s=hc["interval_s"], max_interval_s=hc["max_interval_s"],
            timeout_s=hc["timeout_s"], fail_threshold=hc["fail_threshold"],
            recover_threshold=hc["recover_threshold"], degraded_latency_ms=hc["degraded_latency_ms"],
        ), self.connections)
        self.routes = RouteManager(self.audit, self.connections, self.health.state_of)
        self.rotation = RotationController(self.routes, self.audit)
        self.executor = TaskExecutor(network, self.routes, self.health, self.rotation, self.audit, self.connection)
        self.health.on_change(self._on_health_change)
        sc = cfg.get("sessions") or {}
        self.sessions = SessionRegistry(self.audit, self._session_network_state,
                                        lambda cid: self.connection(cid) is not None,
                                        max_sessions=int(sc.get("max", 2)),
                                        default_timeout_s=float(sc.get("heartbeat_timeout_s", 30.0)))
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- inventario -----------------------------------------------------
    def refresh(self) -> list[Connection]:
        try:
            found = self.discovery.discover()
            self.last_discovery_error = None
        except Exception as e:
            self.last_discovery_error = f"{type(e).__name__}: {e}"
            self.audit.record("error", f"Fallo en detección de interfaces: {e}")
            return self.connections()
        d = self.cfg["discovery"]
        excl = set(d.get("exclude") or [])
        only = set(d.get("only") or [])
        kept = []
        for c in found:
            if c.id in excl or c.name in excl:
                continue
            if only and c.id not in only and c.name not in only:
                continue
            if c.is_virtual and not d.get("include_virtual"):
                continue
            kept.append(c)
        self._apply_egress_check(kept)
        analyze(kept)
        with self._conn_lock:
            old = self._conns
            self._conns = {c.id: c for c in kept}
        for c in kept:
            prev = old.get(c.id)
            if prev is None:
                self.audit.record("inventory.added", f"Conexión detectada: {c.name} ({c.kind.value}, {c.link.value})",
                                  connection_id=c.id, ip=c.primary_ip, gateway=c.gateway, independence=c.independence.value)
            elif (prev.link, prev.ipv4, prev.gateway) != (c.link, c.ipv4, c.gateway):
                self.audit.record("inventory.changed", f"Cambio en {c.name}: enlace {prev.link.value}->{c.link.value}, IP {prev.primary_ip}->{c.primary_ip}",
                                  connection_id=c.id)
        for cid in set(old) - set(self._conns):
            self.audit.record("inventory.removed", f"Conexión desaparecida: {old[cid].name}", connection_id=cid)
        self.sessions.evaluate()
        return kept

    def _apply_egress_check(self, conns: list[Connection]) -> None:
        """Si se configuró un endpoint PROPIO que devuelve la IP pública, se consulta por
        cada conexión (enlazando a su IP). Sin él, la IP pública queda sin verificar."""
        url = self.cfg.get("egress_check_url")
        if not url or self.mode != "real":
            return
        from .net import ConnectFailed, LabRequest, ResponseLost
        for c in conns:
            if not c.usable or c.public_ip:
                continue
            key = (c.id, c.primary_ip)
            cached = self._egress_cache.get(key)
            if cached and time.time() - cached[1] < 600:
                c.public_ip = cached[0]
                continue
            try:
                r = self.network.request(c, LabRequest("GET", url, timeout=5))
                ip = r.body.decode("utf-8", "replace").strip()[:45]
                if r.status == 200 and r.local_ip == c.primary_ip and ip.count(".") == 3:
                    c.public_ip = ip
                    self._egress_cache[key] = (ip, time.time())
            except (ConnectFailed, ResponseLost) as e:
                self.audit.record("inventory.egress_check_failed", f"No se pudo verificar IP pública de {c.name}: {e}", connection_id=c.id)

    def connections(self) -> list[Connection]:
        with self._conn_lock:
            return list(self._conns.values())

    def connection(self, conn_id: str) -> Optional[Connection]:
        with self._conn_lock:
            return self._conns.get(conn_id)

    def connection_view(self, c: Connection) -> dict[str, Any]:
        d = c.to_dict()
        d["health"] = self.health.snapshot().get(c.id, {"state": Health.UNKNOWN.value})
        d["available"] = self.routes.is_available(c.id)
        d["operator_selected"] = self.routes.operator_choice == c.id
        d["active_leases"] = sum(1 for l in self.routes.leases() if l.connection_id == c.id)
        return d

    def _session_network_state(self, conn_id: Optional[str]) -> str:
        """Estado de red atribuible a una sesión: "ok" | "degraded" | "down" | "unknown".
        Solo lee el estado en memoria (no lanza sondeos)."""
        if conn_id is None:
            from .status import connectivity_view
            st = connectivity_view(self, time.time())["state"]
            return {"usable": "ok", "degraded": "degraded", "unknown": "unknown"}.get(st, "down")
        c = self.connection(conn_id)
        if c is None or not c.usable:
            return "down"
        h = self.health.state_of(conn_id)
        return {Health.HEALTHY: "ok", Health.DEGRADED: "degraded", Health.DOWN: "down"}.get(h, "unknown")

    # --- reacción a cambios de salud -------------------------------------
    def _on_health_change(self, conn_id: str, old: Health, new: Health) -> None:
        try:
            self._react_health(conn_id, old, new)
        finally:
            self.sessions.evaluate()

    def _react_health(self, conn_id: str, old: Health, new: Health) -> None:
        if new == Health.DOWN:
            for lease in self.routes.leases():
                if lease.connection_id == conn_id:
                    self.routes.failover(lease, f"pérdida de conectividad confirmada en {conn_id}")
        elif new in (Health.HEALTHY, Health.DEGRADED) and old == Health.DOWN:
            self.audit.record("health.recovered", f"{conn_id} recuperada", connection_id=conn_id)
            for lease in self.routes.leases():
                if lease.preferred_connection_id == conn_id:
                    self.routes.failback_if_possible(lease)

    # --- tareas ---------------------------------------------------------
    def _check_lab_url(self, url: str) -> None:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise EngineError("invalid_url", f"URL inválida: {url}")
        if u.hostname not in self.cfg["lab"]["allowed_hosts"]:
            raise EngineError("target_not_allowed",
                              f"{u.hostname} no está en lab.allowed_hosts; solo se permiten destinos de laboratorio propios", 403)

    def start_task(self, spec: dict[str, Any], run_async: bool = True):
        owner = str(spec.get("owner") or "").strip()
        if not owner:
            raise EngineError("invalid_request", "owner es obligatorio")
        raw_steps = spec.get("steps") or []
        if not raw_steps or len(raw_steps) > 1000:
            raise EngineError("invalid_request", "steps debe tener entre 1 y 1000 pasos")
        steps = []
        for s in raw_steps:
            if not isinstance(s, dict) or "url" not in s:
                raise EngineError("invalid_request", "cada paso necesita url")
            self._check_lab_url(s["url"])
            steps.append(Step(method=s.get("method", "GET"), url=s["url"], body=s.get("body"),
                              idempotent=s.get("idempotent"), idempotency_key=s.get("idempotency_key")))
        policy = spec.get("rotation_policy")
        if policy:
            try:
                self.rotation.get(policy)
            except Exception as e:
                raise EngineError("unknown_policy", str(e))
        try:
            lease = self.routes.acquire(owner, spec.get("name", "tarea"), spec.get("connection_id"),
                                        allow_failover=bool(spec.get("allow_failover", True)),
                                        allow_failback=bool(spec.get("allow_failback", True)))
        except RouteError as e:
            raise EngineError(e.code, e.message, 409)
        task = self.executor.create(
            spec.get("name", "tarea"), owner, steps, lease,
            max_attempts=int(spec.get("max_attempts", 3)),
            step_timeout_s=float(spec.get("step_timeout_s", 10)),
            max_duration_s=float(spec.get("max_duration_s", 300)),
            step_delay_s=float(spec.get("step_delay_s", 0)),
            rotation_policy=policy,
        )
        if policy:
            try:
                self.rotation.attach(policy, lease)
            except Exception as e:
                self.executor.stop(task.id, str(e))
                raise EngineError("policy_error", str(e))
        if run_async:
            self.executor.start(task)
        else:
            self.executor.run(task)
        return task

    def stop_all(self, reason: str = "parada general solicitada") -> dict:
        n = self.executor.stop_all(reason)
        self.rotation.stop_all(reason)
        self.audit.record("engine.stop_all", f"Parada general: {n} tareas detenidas", reason=reason)
        return {"tasks_stopped": n}

    # --- diagnóstico ----------------------------------------------------
    def diagnostics(self) -> dict[str, Any]:
        tasks = self.executor.all()
        return {
            "version": __version__, "mode": self.mode, "uptime_s": round(time.time() - self.started_at, 1),
            "discovery_provider": getattr(self.discovery, "name", "?"),
            "last_discovery_error": self.last_discovery_error,
            "health_targets": self.health.cfg.targets,
            "lab_allowed_hosts": self.cfg["lab"]["allowed_hosts"],
            "connections": len(self._conns),
            "active_leases": len(self.routes.leases()),
            "tasks_running": sum(1 for t in tasks if t.state not in TERMINAL_TASK_STATES),
            "tasks_total": len(tasks),
            "audit_last_seq": self.audit.last_seq,
            "browser_sessions": len(self.sessions.all()),
            "global_routes_modified": False,  # el motor nunca modifica la tabla de rutas
            "note": "Simulación: no prueba hardware real" if self.mode == "simulated" else None,
        }

    # --- ciclo de vida --------------------------------------------------
    def _background(self) -> None:
        last_refresh = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_refresh >= self.cfg["discovery"]["refresh_s"]:
                self.refresh()
                last_refresh = now
            try:
                self.sessions.evaluate()
            except Exception as e:
                self.audit.record("error", f"Fallo al evaluar sesiones: {e!r}")
            try:
                self.rotation.tick()
            except Exception as e:
                self.audit.record("error", f"Fallo en rotación: {e!r}")
            self._stop.wait(1.0)

    def start(self) -> None:
        self.audit.record("engine.start", f"Motor iniciado (modo {self.mode})", version=__version__)
        self.refresh()
        self.health.check_all(force=True)
        self.health.start()
        th = threading.Thread(target=self._background, name="cauce-engine", daemon=True)
        th.start()
        self._threads.append(th)

    def shutdown(self, reason: str = "cierre solicitado") -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.executor.stop_all(reason)
        self.executor.join_all(timeout=10)
        self.health.stop()
        for th in self._threads:
            th.join(timeout=5)
        self.audit.record("engine.stop", f"Motor detenido limpiamente ({reason})")
