"""Ejecutor de tareas de laboratorio.

Una tarea es una lista de pasos HTTP contra destinos de laboratorio autorizados.
Reglas:

1. Cada tarea tiene su propio lease de ruta; nunca toca el de otra tarea.
2. Antes de cada paso se toma una "foto" de la conexión del lease. Un cambio
   de ruta (conmutación, rotación, retorno) solo afecta al paso SIGUIENTE,
   nunca a una petición en curso.
3. Error de conexión (la petición no salió): se confirma con un sondeo de
   salud. Si la conexión está realmente caída y la tarea lo permite, se
   conmuta y se reintenta el mismo paso. Si no está caída, se reintenta en la
   misma conexión con espera creciente. Siempre con tope de intentos.
4. Respuesta perdida (la petición pudo llegar): solo se reintenta si el paso
   es idempotente o lleva clave de idempotencia. Si no, la tarea queda en
   ``needs_review``: NO se ejecuta dos veces una operación no idempotente.
5. Limitación del servicio (429, o 503 con Retry-After) o acceso denegado
   (403/451): la tarea se DETIENE. No se conmuta ni se rota para sortearla.
6. Si la IP de origen usada no coincide con la del lease, la tarea se detiene
   (``route_mismatch``): preferimos fallar a enviar tráfico por la ruta
   equivocada.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .audit import AuditLog
from .health import HealthMonitor
from .models import Connection, Health, TaskState, TERMINAL_TASK_STATES
from .net import ConnectFailed, LabRequest, Network, Response, ResponseLost
from .policy import RotationController
from .routing import Lease, RouteManager

THROTTLE_STATUSES = {429}
DENIED_STATUSES = {403, 451}


@dataclass
class Step:
    method: str
    url: str
    body: Optional[str] = None
    idempotent: Optional[bool] = None
    idempotency_key: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.method = self.method.upper()
        if self.idempotent is None:
            self.idempotent = self.method in ("GET", "HEAD", "OPTIONS", "PUT", "DELETE")

    @property
    def safe_to_retry(self) -> bool:
        return bool(self.idempotent or self.idempotency_key)


@dataclass
class Task:
    id: str
    name: str
    owner: str
    steps: list[Step]
    lease: Lease
    max_attempts: int = 3
    step_timeout_s: float = 10.0
    max_duration_s: float = 300.0
    step_delay_s: float = 0.0
    rotation_policy: Optional[str] = None
    state: TaskState = TaskState.PENDING
    reason: Optional[str] = None
    current_step: int = 0
    results: list[dict] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "owner": self.owner, "state": self.state.value,
            "reason": self.reason, "current_step": self.current_step, "total_steps": len(self.steps),
            "results": list(self.results), "lease": self.lease.to_dict(),
            "rotation_policy": self.rotation_policy, "created": self.created, "finished": self.finished,
        }


class TaskExecutor:
    def __init__(self, network: Network, routes: RouteManager, health: HealthMonitor,
                 rotation: RotationController, audit: AuditLog,
                 get_connection: Callable[[str], Optional[Connection]],
                 sleep: Callable[[float], None] = time.sleep):
        self.network = network
        self.routes = routes
        self.health = health
        self.rotation = rotation
        self.audit = audit
        self.get_connection = get_connection
        self.sleep = sleep
        self._tasks: dict[str, Task] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def create(self, name: str, owner: str, steps: list[Step], lease: Lease, **opts) -> Task:
        t = Task(id="tk_" + secrets.token_hex(6), name=name, owner=owner, steps=steps, lease=lease, **opts)
        with self._lock:
            self._tasks[t.id] = t
        self.audit.record("task.created", f"Tarea '{name}' creada con {len(steps)} pasos",
                          task_id=t.id, owner=owner, lease_id=lease.id, connection_id=lease.connection_id)
        return t

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def start(self, task: Task) -> None:
        th = threading.Thread(target=self.run, args=(task,), name=f"cauce-task-{task.id}", daemon=True)
        with self._lock:
            self._threads[task.id] = th
        th.start()

    def stop(self, task_id: str, reason: str = "detenida por el operador") -> Task:
        t = self.get(task_id)
        if not t:
            raise KeyError(task_id)
        if t.state not in TERMINAL_TASK_STATES:
            t.reason = reason
            t.stop_event.set()
            self.audit.record("task.stop_requested", f"Detención solicitada para '{t.name}'", task_id=t.id, reason=reason)
        return t

    def stop_all(self, reason: str) -> int:
        n = 0
        for t in self.all():
            if t.state not in TERMINAL_TASK_STATES:
                self.stop(t.id, reason)
                n += 1
        return n

    def join_all(self, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        for th in list(self._threads.values()):
            th.join(max(0.0, deadline - time.time()))

    # ------------------------------------------------------------------
    def _finish(self, t: Task, state: TaskState, reason: Optional[str]) -> None:
        t.state = state
        if reason:
            t.reason = reason
        t.finished = time.time()
        self.rotation.detach(t.lease.id)
        try:
            self.routes.release(t.lease.id, t.owner)
        except Exception:
            pass
        self.audit.record(f"task.{state.value}", f"Tarea '{t.name}' -> {state.value}: {t.reason or ''}",
                          task_id=t.id, state=state.value, reason=t.reason, completed_steps=t.current_step)

    def run(self, t: Task) -> Task:
        t.state = TaskState.RUNNING
        self.audit.record("task.started", f"Tarea '{t.name}' iniciada", task_id=t.id, connection_id=t.lease.connection_id)
        deadline = time.time() + t.max_duration_s
        while t.current_step < len(t.steps):
            if t.stop_event.is_set():
                return self._finish(t, TaskState.STOPPED, t.reason) or t
            if time.time() > deadline:
                return self._finish(t, TaskState.FAILED, "duración máxima superada") or t
            self.routes.failback_if_possible(t.lease)
            outcome = self._run_step(t, t.steps[t.current_step])
            if outcome is not None:
                state, reason = outcome
                if state == TaskState.THROTTLED:
                    self.rotation.pause_for_lease(t.lease.id, "limitación del servicio: no se rota para sortearla")
                return self._finish(t, state, reason) or t
            t.current_step += 1
            if t.step_delay_s and t.current_step < len(t.steps):
                if t.stop_event.wait(t.step_delay_s):
                    continue
        self._finish(t, TaskState.COMPLETED, None)
        return t

    def _run_step(self, t: Task, step: Step) -> Optional[tuple[TaskState, str]]:
        attempts = 0
        backoff = 0.2
        while True:
            if t.stop_event.is_set():
                return TaskState.STOPPED, t.reason or "detenida por el operador"
            attempts += 1
            with t.lease.lock:
                conn_id, expected_ip = t.lease.connection_id, t.lease.local_ip
            conn = self.get_connection(conn_id)
            if conn is None:
                # El adaptador desapareció del inventario (desconectado/retirado): pérdida auténtica
                if attempts > t.max_attempts or self.routes.failover(t.lease, f"la conexión {conn_id} desapareció") is None:
                    return TaskState.FAILED, f"la conexión {conn_id} desapareció y no hay alternativa permitida"
                continue
            headers = dict(step.headers)
            if step.idempotency_key:
                headers["Idempotency-Key"] = step.idempotency_key
            req = LabRequest(step.method, step.url, step.body.encode() if step.body is not None else None,
                             headers, t.step_timeout_s)
            base = {"step": t.current_step, "attempt": attempts, "method": step.method, "url": step.url,
                    "connection_id": conn_id, "expected_local_ip": expected_ip, "ts": time.time()}
            try:
                resp = self.network.request(conn, req)
            except ConnectFailed as e:
                t.results.append(dict(base, outcome="connect_failed", error=str(e)))
                self.audit.record("task.connect_failed", f"'{t.name}' paso {t.current_step}: no conectó por {conn_id}",
                                  task_id=t.id, connection_id=conn_id, error=str(e), attempt=attempts)
                nxt = self._recover(t, conn, attempts, backoff, reason=f"fallo de conexión: {e}")
                if nxt is not None:
                    return nxt
                backoff = min(backoff * 2, 5.0)
                continue
            except ResponseLost as e:
                t.results.append(dict(base, outcome="response_lost", error=str(e)))
                if not step.safe_to_retry:
                    self.audit.record("task.ambiguous", f"'{t.name}' paso {t.current_step}: resultado incierto en operación no idempotente; no se repite",
                                      task_id=t.id, connection_id=conn_id, error=str(e))
                    return TaskState.NEEDS_REVIEW, "respuesta perdida en operación no idempotente: revisar manualmente si se aplicó"
                nxt = self._recover(t, conn, attempts, backoff, reason=f"respuesta perdida: {e}")
                if nxt is not None:
                    return nxt
                backoff = min(backoff * 2, 5.0)
                continue

            verified = resp.local_ip == expected_ip and (resp.observed_ip is None or self._observed_matches(conn, resp))
            rec = dict(base, outcome="response", status=resp.status, local_ip=resp.local_ip,
                       observed_ip=resp.observed_ip, carried_by=resp.carried_by, route_verified=verified)
            t.results.append(rec)
            if not verified:
                self.audit.record("route.mismatch", f"'{t.name}': el tráfico no salió por la ruta asignada",
                                  task_id=t.id, expected=expected_ip, local_ip=resp.local_ip, observed=resp.observed_ip)
                return TaskState.FAILED, "route_mismatch: el tráfico no usó la conexión asignada"
            self.audit.record("route.verified", f"'{t.name}' paso {t.current_step}: tráfico verificado por {conn_id}",
                              task_id=t.id, connection_id=conn_id, local_ip=resp.local_ip, observed_ip=resp.observed_ip, status=resp.status)
            if resp.status in THROTTLE_STATUSES or (resp.status == 503 and "retry-after" in resp.headers):
                ra = resp.headers.get("retry-after")
                self.audit.record("service.limit", f"'{t.name}': el servicio pidió limitar ({resp.status}); se detiene sin cambiar de conexión",
                                  task_id=t.id, status=resp.status, retry_after=ra, connection_id=conn_id)
                return TaskState.THROTTLED, f"el servicio respondió {resp.status} (Retry-After={ra}); operación detenida sin usar otra conexión"
            if resp.status in DENIED_STATUSES:
                return TaskState.FAILED, f"acceso denegado ({resp.status}); no se intenta por otra conexión"
            if resp.status >= 400:
                return TaskState.FAILED, f"el servicio respondió {resp.status}"
            return None

    def _observed_matches(self, conn: Connection, resp: Response) -> bool:
        # Si conocemos la IP pública de la conexión, el servidor debe ver esa;
        # si no, debe ver la IP local (laboratorio en la misma red).
        expected = conn.public_ip or conn.primary_ip
        return resp.observed_ip == expected

    def _recover(self, t: Task, conn: Connection, attempts: int, backoff: float, reason: str) -> Optional[tuple[TaskState, str]]:
        if self.health.confirm_down(conn):  # confirmar pérdida auténtica antes de conmutar
            with t.lease.lock:
                already_moved = t.lease.connection_id != conn.id
            # El motor pudo haber conmutado ya este lease al detectar la caída
            new = t.lease.connection_id if already_moved else self.routes.failover(t.lease, reason)
            if new is None:
                return TaskState.FAILED, f"conexión {conn.id} caída y sin alternativa permitida"
            if attempts >= t.max_attempts * 2:
                return TaskState.FAILED, "demasiados intentos tras conmutar"
            return None
        if attempts >= t.max_attempts:
            return TaskState.FAILED, f"{reason} (tras {attempts} intentos; la conexión no se confirmó caída)"
        if t.stop_event.wait(backoff):
            return TaskState.STOPPED, t.reason or "detenida por el operador"
        return None
