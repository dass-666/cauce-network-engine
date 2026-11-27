"""Seguimiento de ESTADO de sesiones de navegador autorizadas (solo observación).

Qué hace:
- Un sistema externo (p. ej. Cauce Forms) registra una sesión de navegador que el
  usuario declara autorizada y envía "latidos" (heartbeats) periódicos con un estado
  mínimo: ``ok`` o ``error`` + un código de una lista cerrada.
- El motor combina ese latido con el estado de red que YA mide (salud de la conexión
  indicada o, si no se indica, la conectividad global) y calcula un estado por sesión.
- Cada cambio de estado se anota en la auditoría (``session.state``), visible para
  Monitor por ``/status/events``.

Qué NO hace (a propósito):
- No abre, controla ni automatiza navegadores; no envía formularios ni reservas.
- No encamina el tráfico del navegador por ninguna conexión: el navegador usa la ruta
  del sistema operativo. ``connection_id`` es solo la conexión de la que la sesión
  DEPENDE según quien la registra, para atribuirle el estado de red correcto.
- No asigna IP distintas a cada sesión ni rota direcciones.
- No recibe URL, cookies, contenido de páginas ni datos de formularios: el latido
  rechaza cualquier campo que no esté en la lista blanca.

Aislamiento:
- Cada sesión pertenece a la credencial ``session`` que la registró (una credencial =
  una sesión activa). Otra credencial no puede verla en detalle, enviarle latidos ni
  cerrarla (403 ``not_session_owner``).
- El estado de una sesión se calcula solo con SUS latidos y SU conexión: un fallo en
  otra sesión o en otra conexión no la modifica (lo comprueban las pruebas).
- Persistencia (E5, ``sessions.persist``, activada por omisión): el estado de diagnóstico se
  guarda en ``sessions_state.json`` (ver cauce/session_store.py). Tras un reinicio, las
  sesiones vuelven como «incierta» (``restored``) hasta que su dueño vuelva a latir con la
  misma credencial. Una sesión que tenía una operación en curso, o que ya requería
  intervención humana, vuelve con ``requires_human`` y SOLO una persona (``resolve``) lo
  quita. Las que llevan demasiado sin latir (``restore_max_age_s``) se cierran como
  caducadas sin perder la marca de intervención. Sin persistencia, el dueño recibe 404
  ``unknown_session`` y debe registrarla de nuevo.
- Marcas pendientes (E6): si una sesión que requería intervención humana se cierra sin que
  una persona la revise (caducidad, cierre del dueño, exceso de sesiones al restaurar), su
  marca queda pendiente. El siguiente registro con la misma credencial o la misma etiqueta
  la hereda. Si el archivo de estado no se puede leer, o solo se pudo usar el respaldo
  (más antiguo), queda una marca general que se aplica a todo registro nuevo hasta que una
  persona resuelva alguna sesión. Ni la caducidad, ni un archivo dañado, ni un respaldo,
  ni recuperar la red pueden dar por buena una operación incierta.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .audit import AuditLog
from .session_store import SessionStore, clean_pending, clean_session

# Estados globales de una sesión, de mayor a menor prioridad.
STATES = ("stale", "network_down", "browser_error", "pending", "network_unknown", "network_degraded", "active")
ERROR_CODES = ("page_unreachable", "navigation_timeout", "browser_closed", "session_expired", "network_error", "other")
HEARTBEAT_FIELDS = {"status", "error_code", "operation"}
OPERATIONS = ("idle", "in_progress")      # el dueño dice si hay una operación (p. ej. un envío) a medio hacer
INTERRUPTED = ("network_down", "stale", "browser_error")
HUMAN_ERROR_CODES = ("session_expired",)  # hace falta que una persona vuelva a entrar
RECOVERED_WINDOW_S = 120.0
CLOSED_KEPT = 10
# E4: fase legible para personas (Monitor), derivada del estado. «requiere_intervencion» tiene prioridad.
PHASES = {"pending": "iniciada", "active": "activa", "network_degraded": "activa", "network_down": "perdida_conectividad",
          "browser_error": "interrumpida", "stale": "incierta", "network_unknown": "incierta"}
REGISTER_FIELDS = {"label", "connection_id", "authorized_by_user", "heartbeat_timeout_s"}
HARD_MAX_SESSIONS = 8
MIN_HEARTBEAT_INTERVAL_S = 0.5     # latidos más frecuentes se rechazan (429)
STABILITY_WINDOW_S = 600.0
HEARTBEAT_SAVE_EVERY_S = 30.0      # los instantes de latido se guardan como mucho cada 30 s (los cambios, al momento)
DEFAULT_RESTORE_MAX_AGE_S = 3600.0
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class SessionError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class BrowserSession:
    id: str
    label: str
    owner_token_id: str
    owner_name: str
    connection_id: Optional[str]
    heartbeat_timeout_s: float
    registered_at: float
    last_heartbeat: Optional[float] = None
    reported: Optional[str] = None          # ok | error
    error_code: Optional[str] = None
    heartbeats: int = 0
    state: str = "pending"
    reason: Optional[str] = None
    state_since: float = 0.0
    changes: deque = field(default_factory=lambda: deque(maxlen=200))   # instantes de cambio de estado
    network_state: str = "unknown"
    operation: Optional[str] = None          # idle | in_progress (lo último que dijo el dueño)
    recovered_from: Optional[str] = None
    recovered_at: Optional[float] = None
    requires_human: bool = False             # solo lo levanta el operador (resolve), nunca el dueño ni el motor
    human_reason: Optional[str] = None
    restored: bool = False                   # vuelve de un reinicio del motor y aún no ha latido desde entonces


# Función que devuelve el estado de red de una conexión (o global si es None):
# "ok" | "degraded" | "down" | "unknown"
NetworkStateFn = Callable[[Optional[str]], str]


class SessionRegistry:
    def __init__(self, audit: AuditLog, network_state: NetworkStateFn,
                 connection_exists: Callable[[str], bool], max_sessions: int = 2,
                 default_timeout_s: float = 30.0, clock: Callable[[], float] = time.time,
                 store: Optional[SessionStore] = None, restore_max_age_s: float = DEFAULT_RESTORE_MAX_AGE_S):
        if not (1 <= max_sessions <= HARD_MAX_SESSIONS):
            raise ValueError(f"sessions.max debe estar entre 1 y {HARD_MAX_SESSIONS}")
        self.audit = audit
        self.network_state = network_state
        self.connection_exists = connection_exists
        self.max_sessions = max_sessions
        self.default_timeout_s = default_timeout_s
        self.clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, BrowserSession] = {}
        self._closed: deque = deque(maxlen=CLOSED_KEPT)
        # Marcas de intervención humana sin resolver de sesiones ya cerradas (E6). Sin límite de tamaño a propósito:
        # cada una exige que una persona la revise; están acotadas por max_sessions y por las etiquetas en uso.
        self._pending: list[dict] = []
        self.store = store
        self.restore_max_age_s = float(restore_max_age_s)
        self._last_save = 0.0
        self._dirty = False
        self._write_failed = False
        self._seq = 0                      # número de la última foto tomada para guardar (E7)
        if store is not None:
            self._restore()

    # --- persistencia -------------------------------------------------------
    def _persisted(self, s: BrowserSession) -> dict:
        return {"id": s.id, "label": s.label, "owner_token_id": s.owner_token_id, "owner_name": s.owner_name,
                "connection_id": s.connection_id, "heartbeat_timeout_s": s.heartbeat_timeout_s,
                "registered_at": s.registered_at, "last_heartbeat": s.last_heartbeat, "state": s.state,
                "operation": s.operation, "requires_human": s.requires_human, "human_reason": s.human_reason,
                "heartbeats": s.heartbeats}

    def _save(self, force: bool = True) -> None:
        """Guarda el estado. force=False solo si pasó HEARTBEAT_SAVE_EVERY_S (latidos). Nunca lanza."""
        if self.store is None:
            return
        now = self.clock()
        with self._lock:
            if not force and not (self._dirty and now - self._last_save >= HEARTBEAT_SAVE_EVERY_S):
                return
            sessions = [self._persisted(x) for x in self._sessions.values()]
            closed = list(self._closed)
            pending = [dict(p) for p in self._pending]
            self._dirty, self._last_save = False, now
            # E7: número de foto. El almacén descarta una foto más vieja que la última escrita (dos hilos guardando
            # a la vez podían dejar en disco la foto anterior a «en curso»).
            self._seq += 1
            seq = self._seq
        try:
            self.store.save(sessions, closed, now, pending, seq=seq)
            if self._write_failed:
                self._write_failed = False
                self.audit.record("session.state_saved", "Estado de sesiones guardado de nuevo tras un fallo")
        except OSError as e:
            with self._lock:
                self._dirty = True
            if not self._write_failed:                    # se anota una vez, no en cada intento
                self._write_failed = True
                self.audit.record("session.state_write_failed",
                                  "No se pudo guardar el estado de las sesiones (sigue en memoria)", error=type(e).__name__)

    def _park(self, s: BrowserSession, now: float) -> None:
        """La sesión se cierra con una marca sin resolver: la marca queda pendiente para su próximo registro.
        Llamar con el lock tomado."""
        if s.requires_human:
            self._pending.append({"owner_token_id": s.owner_token_id, "label": s.label,
                                  "reason": s.human_reason or "operation_interrupted", "session_id": s.id, "since": now})

    def _general_mark(self, reason: str, now: float) -> None:
        if not any(p["reason"] == reason and p["owner_token_id"] is None and p["label"] is None for p in self._pending):
            self._pending.append({"owner_token_id": None, "label": None, "reason": reason, "session_id": None,
                                  "since": now})

    def _restore(self) -> None:
        doc, status = self.store.load()
        now = self.clock()
        if status == "corrupt":
            self.audit.record("session.state_corrupt",
                              "Estado de sesiones dañado y sin respaldo válido: se empieza vacío. Toda sesión nueva "
                              "requerirá revisión humana hasta que una persona resuelva una.")
            with self._lock:
                self._general_mark("previous_state_unreadable", now)
            self._save()
            return
        if doc is None:
            return
        if status == "backup":
            self.audit.record("session.state_recovered_from_backup",
                              "Estado de sesiones dañado: se recuperó la copia anterior (puede faltar el último cambio); "
                              "todas las sesiones requieren revisión humana")
        for raw in doc.get("pending") or []:
            p = clean_pending(raw)
            if p is not None:
                self._pending.append(p)
        if status == "backup":
            self._general_mark("state_recovered_from_backup", now)
        for c in doc.get("closed") or []:
            if isinstance(c, dict) and isinstance(c.get("closed_at"), (int, float)):
                self._closed.append(c)
        restored, expired = [], []
        for raw in doc.get("sessions") or []:
            d = clean_session(raw)
            if d is None:
                continue
            ref = d["last_heartbeat"] if d["last_heartbeat"] is not None else d["registered_at"]
            s = BrowserSession(id=d["id"], label=d["label"], owner_token_id=d["owner_token_id"],
                               owner_name=d["owner_name"], connection_id=d["connection_id"],
                               heartbeat_timeout_s=float(d["heartbeat_timeout_s"]), registered_at=d["registered_at"],
                               last_heartbeat=None, heartbeats=d["heartbeats"], state="stale", reason="engine_restarted",
                               state_since=now, operation=d["operation"], requires_human=d["requires_human"],
                               human_reason=d["human_reason"], restored=True)
            s.registered_at = now          # el plazo de latido cuenta desde el reinicio, no desde el registro original
            if d["operation"] == "in_progress" and not s.requires_human:
                # El resultado de esa operación es desconocido: el reinicio NUNCA la da por buena ni la repite.
                s.requires_human, s.human_reason = True, "engine_restart_during_operation"
            if status == "backup" and not s.requires_human:
                # El respaldo es anterior al último cambio: lo que pasó después es desconocido.
                s.requires_human, s.human_reason = True, "state_recovered_from_backup"
            if now - ref > self.restore_max_age_s:
                v = self.view(s)
                self._closed.appendleft({**v, "state": "closed", "phase": "cerrada", "reason": "expired_after_restart",
                                         "closed_at": now})
                self._park(s, now)
                expired.append(s)
                continue
            if len(self._sessions) < self.max_sessions:
                self._sessions[s.id] = s
                restored.append(s)
            else:                                           # p. ej. se redujo sessions.max: nunca se pierde en silencio
                self._closed.appendleft({**self.view(s), "state": "closed", "phase": "cerrada",
                                         "reason": "over_limit_after_restart", "closed_at": now})
                self._park(s, now)
                expired.append(s)
        for s in restored:
            self.audit.record("session.restored", f"Sesión {s.label}: restaurada tras reinicio (incierta hasta su próximo latido)",
                              session_id=s.id, requires_human=s.requires_human, reason=s.human_reason)
            if s.human_reason == "engine_restart_during_operation":
                self.audit.record("session.requires_human",
                                  f"Sesión {s.label}: requiere revisión humana (motor reiniciado con una operación en curso)",
                                  session_id=s.id, reason=s.human_reason)
        for s in expired:
            self.audit.record("session.expired", f"Sesión {s.label}: caducada tras el reinicio", session_id=s.id,
                              requires_human=s.requires_human)
        if restored or expired or status == "backup" or self._pending:
            self._save()

    # --- registro ---------------------------------------------------------
    def register(self, owner_token_id: str, owner_name: str, body: dict[str, Any]) -> BrowserSession:
        extra = sorted(set(body) - REGISTER_FIELDS)
        if extra:
            raise SessionError(400, "unsupported_field", f"Campo no admitido: {', '.join(extra)}")
        if body.get("authorized_by_user") is not True:
            raise SessionError(400, "authorization_not_declared",
                               "authorized_by_user debe ser true: solo se registran sesiones que el usuario declara autorizadas")
        label = str(body.get("label") or "")
        if not _LABEL_RE.match(label):
            raise SessionError(400, "invalid_label", "label: 2-32 caracteres, minúsculas, dígitos, '-' o '_'")
        cid = body.get("connection_id")
        if cid is not None:
            cid = str(cid)
            if not self.connection_exists(cid):
                raise SessionError(404, "unknown_connection", "Conexión inexistente")
        timeout = float(body.get("heartbeat_timeout_s", self.default_timeout_s))
        if not (5.0 <= timeout <= 300.0):
            raise SessionError(400, "invalid_request", "heartbeat_timeout_s debe estar entre 5 y 300")
        now = self.clock()
        inherited, replaced = None, None
        with self._lock:
            prev = next((x for x in self._sessions.values() if x.owner_token_id == owner_token_id), None)
            sustituible = prev is not None and prev.restored
            # E7: TODAS las comprobaciones van antes de tocar la sesión restaurada. Antes se borraba primero y un 409
            # posterior (p. ej. etiqueta en uso) se llevaba la sesión y su marca de intervención humana.
            otras = [x for x in self._sessions.values() if not (sustituible and x is prev)]
            if any(x.owner_token_id == owner_token_id for x in otras):
                raise SessionError(409, "credential_has_session",
                                   "Esta credencial ya tiene una sesión activa (una credencial = una sesión)")
            if any(x.label == label for x in otras):
                raise SessionError(409, "label_in_use", "Ya existe una sesión con esa etiqueta")
            if len(otras) >= self.max_sessions:
                raise SessionError(409, "too_many_sessions", f"Máximo {self.max_sessions} sesiones")
            if sustituible:
                # El dueño perdió su id al reiniciarse él también: la sesión restaurada se sustituye, pero su
                # marca de intervención humana PASA a la nueva (un nuevo registro nunca la borra).
                del self._sessions[prev.id]
                self._closed.appendleft({**self.view(prev), "state": "closed", "phase": "cerrada",
                                         "reason": "replaced_after_restart", "closed_at": now})
                replaced = prev
                if prev.requires_human:
                    inherited = (prev.human_reason, prev.operation)
            s = BrowserSession(id="bs_" + secrets.token_hex(6), label=label, owner_token_id=owner_token_id,
                               owner_name=owner_name, connection_id=cid, heartbeat_timeout_s=timeout,
                               registered_at=now, state_since=now)
            if inherited is not None:
                s.requires_human, s.human_reason = True, inherited[0]
            # Marcas pendientes de sesiones cerradas sin revisar (misma credencial o misma etiqueta) y marcas
            # generales (estado ilegible o recuperado del respaldo): un registro nuevo nunca las borra.
            mine = [p for p in self._pending if p["owner_token_id"] == owner_token_id or p["label"] == label]
            general = [p for p in self._pending if p["owner_token_id"] is None and p["label"] is None]
            if mine or general:
                if not s.requires_human:
                    s.requires_human, s.human_reason = True, (mine or general)[0]["reason"]
                self._pending = [p for p in self._pending if p not in mine]   # pasan a la sesión nueva
                pending_used = len(mine) + len(general)
            else:
                pending_used = 0
            self._sessions[s.id] = s
        if replaced is not None:
            self.audit.record("session.replaced", f"Sesión {replaced.label}: sustituida por un nuevo registro de su dueño "
                              "tras el reinicio", session_id=replaced.id, new_session_id=s.id,
                              requires_human=replaced.requires_human)
        self.audit.record("session.registered", f"Sesión de navegador registrada: {label}",
                          session_id=s.id, label=label, connection_id=cid, registered_by=owner_name)
        if pending_used:
            self.audit.record("session.requires_human",
                              f"Sesión {label}: hereda una revisión humana pendiente ({s.human_reason})",
                              session_id=s.id, reason=s.human_reason)
        self.evaluate()
        self._save()
        return s

    def _owned(self, sid: str, token_id: str, admin: bool = False) -> BrowserSession:
        s = self._sessions.get(sid)
        if not s:
            raise SessionError(404, "unknown_session", "Sesión inexistente (¿se reinició el motor?)")
        if not admin and s.owner_token_id != token_id:
            raise SessionError(403, "not_session_owner", "Esta sesión pertenece a otra credencial")
        return s

    def heartbeat(self, sid: str, token_id: str, body: dict[str, Any]) -> BrowserSession:
        extra = sorted(set(body) - HEARTBEAT_FIELDS)
        if extra:
            raise SessionError(400, "unsupported_field",
                               f"Campo no admitido en el latido: {', '.join(extra)} (no se aceptan URL, cookies ni datos de página)")
        status = body.get("status")
        if status not in ("ok", "error"):
            raise SessionError(400, "invalid_request", "status debe ser 'ok' o 'error'")
        code = body.get("error_code")
        if status == "ok" and code is not None:
            raise SessionError(400, "invalid_request", "error_code solo se admite con status 'error'")
        if status == "error" and code not in ERROR_CODES:
            raise SessionError(400, "invalid_error_code", f"error_code debe ser uno de: {', '.join(ERROR_CODES)}")
        op = body.get("operation")
        if op is not None and op not in OPERATIONS:
            raise SessionError(400, "invalid_request", f"operation debe ser uno de: {', '.join(OPERATIONS)}")
        now = self.clock()
        with self._lock:
            s = self._owned(sid, token_id)
            if s.last_heartbeat is not None and now - s.last_heartbeat < MIN_HEARTBEAT_INTERVAL_S:
                raise SessionError(429, "heartbeat_too_frequent",
                                   f"Latidos como máximo cada {MIN_HEARTBEAT_INTERVAL_S} s")
            s.last_heartbeat, s.reported, s.error_code = now, status, code
            op_changed = op is not None and op != s.operation
            if op is not None:
                s.operation = op
            s.heartbeats += 1
            was_restored, s.restored = s.restored, False
            self._dirty = True
        self.evaluate()
        # un cambio de operación se guarda al momento: si el motor cae justo después, la marca «en curso» no se pierde
        self._save(force=op_changed or was_restored)
        return s

    def close(self, sid: str, token_id: str, admin: bool = False, reason: str = "cerrada por su dueño") -> dict:
        with self._lock:
            s = self._owned(sid, token_id, admin)
            del self._sessions[sid]
            v = self.view(s)
            self._closed.appendleft({**v, "state": "closed", "phase": "cerrada", "reason": reason,
                                     "closed_at": self.clock()})
            self._park(s, self.clock())
        self.audit.record("session.closed", f"Sesión de navegador cerrada: {s.label} ({reason})",
                          session_id=s.id, label=s.label, requires_human=s.requires_human)
        self._save()
        return {"closed": s.id}

    def resolve(self, sid: str) -> BrowserSession:
        """Una PERSONA (credencial admin, el operador) confirma que revisó la sesión. Nada se repite solo."""
        with self._lock:
            s = self._owned(sid, "", admin=True)
            was, s.requires_human, s.human_reason = s.human_reason, False, None
            # La persona revisó la situación: las marcas generales (estado ilegible o del respaldo) quedan atendidas.
            general = [p for p in self._pending if p["owner_token_id"] is None and p["label"] is None]
            self._pending = [p for p in self._pending if p not in general]
        self.audit.record("session.human_resolved", f"Sesión {s.label}: revisada por el operador", session_id=s.id,
                          reason=was, general_marks_cleared=len(general))
        self._save()
        return s

    def get(self, sid: str, token_id: str, admin: bool = False) -> BrowserSession:
        with self._lock:
            return self._owned(sid, token_id, admin)

    def all(self) -> list[BrowserSession]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda s: s.label)

    # --- cálculo de estado --------------------------------------------------
    def _compute(self, s: BrowserSession, now: float) -> tuple[str, Optional[str], str]:
        net = self.network_state(s.connection_id)
        if s.connection_id is not None and not self.connection_exists(s.connection_id):
            net = "down"
        if s.restored:
            return "stale", "engine_restarted", net
        ref = s.last_heartbeat if s.last_heartbeat is not None else s.registered_at
        if now - ref > s.heartbeat_timeout_s:
            return "stale", "no_heartbeat" if s.last_heartbeat is None else "heartbeat_timeout", net
        if net == "down":
            return "network_down", "connection_down" if s.connection_id else "no_usable_connection", net
        if s.reported == "error":
            return "browser_error", s.error_code, net
        if s.last_heartbeat is None:
            return "pending", "awaiting_first_heartbeat", net
        if net == "unknown":
            return "network_unknown", "insufficient_measurements", net
        if net == "degraded":
            return "network_degraded", None, net
        return "active", None, net

    def evaluate(self) -> None:
        """Recalcula todas las sesiones y anota cada transición. Barato: no hace E/S de red."""
        now = self.clock()
        changes, expired = [], []
        with self._lock:
            for s in list(self._sessions.values()):
                ref = s.last_heartbeat if s.last_heartbeat is not None else s.registered_at
                if now - ref > self.restore_max_age_s:
                    if s.operation == "in_progress" and not s.requires_human:
                        s.requires_human, s.human_reason = True, "operation_interrupted"
                    # Sin latidos durante demasiado tiempo: se cierra como caducada (no se autoriza ni repite nada;
                    # la marca de intervención humana se conserva en el historial y en la auditoría).
                    del self._sessions[s.id]
                    self._closed.appendleft({**self.view(s), "state": "closed", "phase": "cerrada",
                                             "reason": "expired", "closed_at": now})
                    self._park(s, now)
                    expired.append(s)
            for s in self._sessions.values():
                state, reason, net = self._compute(s, now)
                s.network_state = net
                if state != s.state or reason != s.reason:
                    old = s.state
                    if state != old:
                        s.state_since = now
                        s.changes.append(now)
                        changes.append((s.id, s.label, old, state, reason))
                    s.state, s.reason = state, reason
        flagged, recovered = [], []
        with self._lock:
            for sid, label, old, new, reason in changes:
                s = self._sessions.get(sid)
                if s is None:
                    continue
                if new == "browser_error" and reason in HUMAN_ERROR_CODES and not s.requires_human:
                    s.requires_human, s.human_reason = True, reason
                    flagged.append((sid, label, reason))
                if new == "active" and old in INTERRUPTED + ("network_unknown",):
                    s.recovered_from, s.recovered_at = old, now
                    recovered.append((sid, label, old))
            # Una operación en curso con la sesión interrumpida (porque se cortó durante la operación o porque la
            # persona la empezó ya cortada; hallazgo de E5 con las capturas) tiene resultado desconocido: NO se repite.
            for s in self._sessions.values():
                if s.state in INTERRUPTED and s.operation == "in_progress" and not s.requires_human:
                    s.requires_human, s.human_reason = True, "operation_interrupted"
                    flagged.append((s.id, s.label, s.human_reason))
        for sid, label, old, new, reason in changes:
            self.audit.record("session.state", f"Sesión {label}: {old} -> {new}",
                              session_id=sid, old=old, new=new, reason=reason)
        for sid, label, why in flagged:
            self.audit.record("session.requires_human", f"Sesión {label}: requiere revisión humana ({why})",
                              session_id=sid, reason=why)
        for sid, label, old in recovered:
            self.audit.record("session.recovered", f"Sesión {label}: recuperada tras {old}", session_id=sid, previous=old)
        for s in expired:
            self.audit.record("session.expired", f"Sesión {s.label}: caducada por falta de latidos", session_id=s.id,
                              requires_human=s.requires_human)
        if changes or flagged or expired:
            self._save()
        else:
            self._save(force=False)

    # --- vistas -------------------------------------------------------------
    def view(self, s: BrowserSession) -> dict[str, Any]:
        """Lista blanca de campos. No incluye el id de la credencial dueña. Usa el mismo reloj que el registro."""
        now = self.clock()
        recent = sum(1 for t in s.changes if now - t <= STABILITY_WINDOW_S)
        return {
            "id": s.id,
            "label": s.label,
            "registered_by": s.owner_name,
            "state": s.state,
            "reason": s.reason,
            "state_since_s": round(now - s.state_since, 1),
            "network": {"source": "connection" if s.connection_id else "global",
                        "connection_id": s.connection_id, "state": s.network_state},
            "heartbeat": {"last_age_s": None if s.last_heartbeat is None else round(now - s.last_heartbeat, 1),
                          "timeout_s": s.heartbeat_timeout_s, "reported": s.reported,
                          "error_code": s.error_code, "count": s.heartbeats},
            "stability": {"state_changes_10m": recent, "state_changes_total": len(s.changes)},
            "phase": self.phase(s, now),
            "operation": s.operation,
            "requires_human": s.requires_human,
            "human_reason": s.human_reason,
            "restored": s.restored,
            "recovered": ({"from": s.recovered_from, "age_s": round(now - s.recovered_at, 1)}
                          if s.recovered_at is not None and now - s.recovered_at <= RECOVERED_WINDOW_S else None),
        }

    @staticmethod
    def phase(s: BrowserSession, now: float) -> str:
        if s.requires_human:
            return "requiere_intervencion"
        if s.state == "active" and s.recovered_at is not None and now - s.recovered_at <= RECOVERED_WINDOW_S:
            return "recuperada"
        return PHASES.get(s.state, "incierta")

    def summary(self) -> dict[str, Any]:
        with self._lock:
            ss = self.all()
            conns = [s.connection_id for s in ss if s.connection_id]
            return {
                "max_sessions": self.max_sessions,
                # marcas de intervención humana de sesiones ya cerradas; se heredan al registrar (sin ids ni dueños)
                "pending_human_review": [{"label": p["label"], "reason": p["reason"]} for p in self._pending],
                "sessions": [self.view(s) for s in ss],
                "closed": [{k: v for k, v in c.items() if k != "closed_at"} | {"closed_age_s": round(self.clock() - c["closed_at"], 1)}
                           for c in self._closed],
                "isolation": {
                    # siempre true por diseño (registro rechaza lo contrario); se expone para que Monitor lo vigile
                    "one_credential_per_session": len({s.owner_token_id for s in ss}) == len(ss),
                    # informativo: dos sesiones que dependen de la misma conexión caen a la vez si esta cae
                    "shared_connection": len(conns) != len(set(conns)),
                },
            }
