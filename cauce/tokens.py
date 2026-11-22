"""Credenciales con permisos mínimos para consumidores de la API local.

Modelo (resumen; detalles y límites en docs/LECTURA_PROTEGIDA.md):

- ``admin``: el token de sesión aleatorio de cada arranque (api.py). Nunca se
  entrega a un consumidor. Es el único que puede crear y revocar credenciales.
- ``read``: credenciales persistentes, una por sistema consumidor, que solo
  pueden llamar a los endpoints marcados como ``read`` en el servidor.

Del token ``read`` solo se guarda su hash SHA-256 (es un secreto aleatorio de
256 bits, así que no necesita un hash lento). El texto del token se muestra una
única vez, al crearlo. El registro se guarda con el mismo almacén protegido que
el token de sesión (DPAPI en Windows; archivo 0600 sin cifrar en otros sistemas).

Formato del token: ``cr1.<id>.<secreto>``. El id (no secreto) permite localizar
el registro y anotarlo en el registro de accesos sin escribir nunca el secreto.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .secrets_store import read_secret, write_secret

SCOPE_READ = "read"
SCOPE_ADMIN = "admin"
SCOPE_SESSION = "session"   # solo rutas /sessions: registrar UNA sesión de navegador y enviarle latidos
CREATABLE_SCOPES = (SCOPE_READ, SCOPE_SESSION)  # un token admin nunca se crea por la API
MAX_TOKENS = 20
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_TOKEN_RE = re.compile(r"^cr1\.([0-9a-f]{8})\.([A-Za-z0-9_-]{20,128})$")


class TokenError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class Principal:
    scope: str
    token_id: str   # "session" para el token de administración
    name: str       # nombre del sistema consumidor


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class TokenStore:
    """Registro de credenciales con persistencia VERIFICADA y bloqueo de fallo seguro.

    Principio: una revocación solo se informa como completada cuando está en disco y se ha releído.
    Si no puede garantizarse (fallo de escritura, de reemplazo, de relectura o archivo ilegible), el almacén
    pasa a estado BLOQUEADO: ninguna credencial ``read`` es válida (503 ``credentials_locked``) hasta que se
    compruebe una escritura completa, de modo que no se sigue confiando en un registro del que no se sabe si es
    el último. El token de administración (de sesión) no depende de este almacén y no se ve afectado.

    Escritura: archivo temporal + fsync + reemplazo atómico + relectura y comparación con lo esperado.
    Archivo de bloqueo ``tokens.dat.lock`` (solo ids, sin secretos): se intenta escribir en cuanto falla el guardado, por
    un camino independiente del reemplazo, y SOBREVIVE a reinicios: al arrancar con él, el almacén sigue bloqueado y
    reaplica las revocaciones pendientes que contenga. Solo se borra tras una escritura verificada.

    Límite que ningún software evita: si el disco no admite NINGUNA escritura (ni el registro ni el archivo de
    bloqueo) y el proceso muere, no queda constancia de la revocación. Mientras el motor siga vivo permanece bloqueado
    y la operación se informó como NO completada; se reintenta solo cada ``RETRY_S`` segundos.
    """

    RETRY_S = 5.0

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._tokens: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, dict[str, Any]] = {}   # solo en memoria
        self._locked: Optional[str] = None            # motivo del bloqueo, o None
        self._pending: set[str] = set()               # revocaciones vigentes en memoria que aún no están verificadas en disco
        self._marker_written = False
        self._last_retry = time.monotonic()   # el primer reintento automático espera RETRY_S
        if path:
            self._startup()

    # --- arranque -------------------------------------------------------
    @property
    def _marker(self) -> str:
        return self._path + ".lock"

    def _startup(self) -> None:
        loaded = True
        if os.path.exists(self._path):
            try:
                data = json.loads(read_secret(self._path))
                self._tokens = {t["id"]: t for t in data.get("tokens", [])}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                loaded = False
                self._tokens = {}
                self._locked = "store_unreadable"
        if os.path.exists(self._marker):
            # Un fallo anterior dejó constancia: se reaplican las revocaciones pendientes y se sigue bloqueado.
            try:
                with open(self._marker, encoding="utf-8") as fh:
                    info = json.load(fh)
                pend = {i for i in info.get("pending", []) if isinstance(i, str)}
            except (OSError, ValueError, AttributeError):
                pend = set()
            now = round(time.time(), 3)
            for i in pend:
                if i in self._tokens and not self._tokens[i].get("revoked_at"):
                    self._tokens[i]["revoked_at"] = now
            self._pending |= pend
            self._marker_written = True
            if self._locked is None:
                self._locked = "persist_failed"
        elif loaded and self._tokens and self._locked is None:
            # Sonda de escritura: si el registro existente no se puede reescribir y verificar, no se confía en él.
            try:
                self._write_verified()
            except TokenError:
                self._locked = "storage_not_writable"

    # --- persistencia ---------------------------------------------------
    def _snapshot(self) -> dict[str, Any]:
        return {"version": 1, "tokens": list(self._tokens.values())}

    def _write_verified(self) -> None:
        """Escribe de forma atómica y comprueba por relectura. Lanza TokenError(503) si algo falla."""
        if not self._path:
            return
        tmp = self._path + ".tmp"
        expected = self._snapshot()
        try:
            write_secret(tmp, json.dumps(expected))
            os.replace(tmp, self._path)
            self._fsync_dir()
            back = json.loads(read_secret(self._path))
            if back != json.loads(json.dumps(expected)):
                raise OSError("la relectura no coincide con lo escrito")
        except (OSError, ValueError) as e:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise TokenError(503, "storage_error",
                             f"No se pudo guardar y verificar el registro de credenciales: {e.__class__.__name__}")

    def _fsync_dir(self) -> None:
        if os.name == "nt":
            return
        fd = os.open(os.path.dirname(os.path.abspath(self._path)), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_marker(self) -> bool:
        """Constancia durable del bloqueo y de las revocaciones pendientes (ids, sin secretos). Sin reemplazo de archivos."""
        try:
            fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"reason": self._locked, "pending": sorted(self._pending), "ts": round(time.time(), 3)}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except OSError:
            return False

    def _lock_down(self, reason: str) -> None:
        self._locked = reason
        self._last_retry = time.monotonic()
        self._marker_written = self._write_marker()

    def _persist_or_lock(self, reason: str) -> None:
        """Intenta dejar el estado actual en disco; si no, bloquea. Lanza TokenError(503) en ese caso."""
        try:
            self._write_verified()
        except TokenError as e:
            self._lock_down(reason)
            raise e
        self._unlock_if_clean()

    def _unlock_if_clean(self) -> None:
        """Tras una escritura verificada que incluye las revocaciones pendientes, se levanta el bloqueo."""
        self._pending.clear()
        self._locked = None
        self._marker_written = False
        with contextlib.suppress(OSError):
            os.remove(self._marker)

    def _auto_retry(self) -> None:
        """Reintento espaciado (solo si el bloqueo fue por fallo de guardado, nunca por archivo ilegible)."""
        if self._locked != "persist_failed" or time.monotonic() - self._last_retry < self.RETRY_S:
            return
        self._last_retry = time.monotonic()
        with contextlib.suppress(TokenError):
            self._write_verified()
            self._unlock_if_clean()

    @property
    def locked(self) -> Optional[str]:
        with self._lock:
            return self._locked

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"locked": self._locked, "pending_revocations": sorted(self._pending),
                    "lock_recorded_on_disk": self._marker_written}

    # --- administración -------------------------------------------------
    def create(self, name: str, scope: str = SCOPE_READ) -> tuple[dict[str, Any], str]:
        if scope not in CREATABLE_SCOPES:
            raise TokenError(400, "invalid_scope", f"Permiso no permitido: {scope!r} (solo {', '.join(CREATABLE_SCOPES)})")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise TokenError(400, "invalid_name", "Nombre inválido: 2-32 caracteres, minúsculas, dígitos, '-' o '_'")
        with self._lock:
            if self._locked:
                raise TokenError(503, "credentials_locked", self._locked_msg())
            active = [t for t in self._tokens.values() if not t.get("revoked_at")]
            if any(t["name"] == name for t in active):
                raise TokenError(409, "name_in_use", f"Ya existe una credencial activa llamada {name!r}")
            if len(active) >= MAX_TOKENS:
                raise TokenError(409, "too_many_tokens", f"Máximo {MAX_TOKENS} credenciales activas")
            tid = secrets.token_hex(4)
            while tid in self._tokens:
                tid = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            rec = {"id": tid, "name": name, "scope": scope, "created_at": round(time.time(), 3),
                   "revoked_at": None, "hash": _hash(secret)}
            self._tokens[tid] = rec
            try:
                self._write_verified()
            except TokenError:
                del self._tokens[tid]   # no queda una credencial cuyo secreto nadie recibió ni que sobreviva a un reinicio
                raise
        return self._public(rec), f"cr1.{tid}.{secret}"

    def revoke(self, token_id: str) -> dict[str, Any]:
        """Éxito = revocación VERIFICADA en disco. Si no, bloquea el almacén y lanza 503 ``revocation_not_persisted``."""
        with self._lock:
            rec = self._tokens.get(token_id)
            if not rec:
                raise TokenError(404, "unknown_token", "Credencial inexistente")
            if rec.get("revoked_at") and token_id not in self._pending and not self._locked:
                return self._public(rec)   # ya estaba revocada y verificada
            rec["revoked_at"] = rec.get("revoked_at") or round(time.time(), 3)
            self._pending.add(token_id)
            try:
                if self._locked == "store_unreadable":
                    raise TokenError(503, "storage_error", "Registro ilegible: use recover")
                self._persist_or_lock("persist_failed")
            except TokenError as e:
                if not self._locked:
                    self._lock_down("persist_failed")
                raise TokenError(503, "revocation_not_persisted",
                                 "REVOCACIÓN NO COMPLETADA: no se pudo guardar y verificar en disco. Por seguridad las "
                                 "credenciales read quedan bloqueadas (credentials_locked) hasta que el almacenamiento "
                                 "funcione; se reintenta solo, o ejecute 'cauce token recover'. "
                                 + ("Quedó constancia del bloqueo en disco." if self._marker_written
                                    else "NO se pudo dejar constancia en disco: no reinicie el motor.")) from e
            return self._public(rec)

    def recover(self) -> dict[str, Any]:
        """Acción explícita del administrador: reescribe y verifica el registro y levanta el bloqueo si todo va bien.

        Si el registro estaba ilegible, se aparta como ``.unreadable`` y se empieza vacío: las credenciales anteriores
        quedan INVALIDADAS (no se reutilizan credenciales de las que no se puede verificar el estado) y hay que recrearlas.
        """
        with self._lock:
            if self._locked == "store_unreadable":
                with contextlib.suppress(OSError):
                    os.replace(self._path, self._path + ".unreadable")
                self._tokens = {}
            if self._locked is None and not self._pending:
                return {"recovered": False, "locked": None, "note": "El almacén no estaba bloqueado."}
            try:
                self._write_verified()
            except TokenError as e:
                if not self._locked:
                    self._lock_down("persist_failed")
                raise TokenError(503, "credentials_locked", "El almacenamiento sigue sin poder verificarse: " + e.message) from e
            self._unlock_if_clean()
            return {"recovered": True, "locked": None}

    def _locked_msg(self) -> str:
        return ("Credenciales read bloqueadas por seguridad: el almacenamiento no pudo verificarse "
                f"({self._locked}). Un administrador debe resolverlo ('cauce token recover').")

    def _public(self, rec: dict[str, Any]) -> dict[str, Any]:
        u = self._usage.get(rec["id"], {})
        return {"id": rec["id"], "name": rec["name"], "scope": rec["scope"], "created_at": rec["created_at"],
                "revoked_at": rec.get("revoked_at"), "revocation_pending": rec["id"] in self._pending,
                "last_used": u.get("last_used"), "requests": u.get("requests", 0)}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(t) for t in self._tokens.values()]

    # --- verificación ---------------------------------------------------
    def verify(self, presented: str) -> Principal:
        """Devuelve el Principal o lanza TokenError (401/503). Nunca registra ``presented``."""
        m = _TOKEN_RE.match(presented or "")
        if not m:
            raise TokenError(401, "unauthorized", "Falta token válido (Authorization: Bearer ...)")
        tid, secret = m.groups()
        with self._lock:
            self._auto_retry()
            if self._locked:
                raise TokenError(503, "credentials_locked", self._locked_msg())
            rec = self._tokens.get(tid)
            expected = rec["hash"] if rec else "0" * 64   # misma comparación aunque no exista
            ok = hmac.compare_digest(_hash(secret), expected)
            if not rec or not ok:
                raise TokenError(401, "unauthorized", "Falta token válido (Authorization: Bearer ...)")
            if rec.get("revoked_at"):
                raise TokenError(401, "token_revoked", "Credencial revocada")
            u = self._usage.setdefault(tid, {"requests": 0, "last_used": None})
            u["requests"] += 1
            u["last_used"] = round(time.time(), 3)
            return Principal(rec["scope"], tid, rec["name"])


class AccessLog:
    """Registro de accesos de la API (JSON por línea). Sin secretos ni cuerpos.

    Va aparte de la auditoría con hashes: una lectura cada pocos segundos
    llenaría la memoria de eventos y provocaría huecos de secuencia a los consumidores.
    """

    def __init__(self, path: Optional[str], max_bytes: int = 5_000_000, keep: int = 3):
        self._path, self._max, self._keep = path, max_bytes, keep
        self._lock = threading.Lock()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def record(self, principal: Optional[Principal], method: str, path: str, status: int, code: str = "") -> None:
        if not self._path:
            return
        line = json.dumps({"ts": round(time.time(), 3), "who": principal.name if principal else None,
                           "token_id": principal.token_id if principal else None,
                           "scope": principal.scope if principal else None,
                           "method": method, "path": path[:200], "status": status, "code": code},
                          ensure_ascii=False)
        with self._lock:
            try:
                if os.path.exists(self._path) and os.path.getsize(self._path) > self._max:
                    for i in range(self._keep - 1, 0, -1):
                        src = f"{self._path}.{i}"
                        if os.path.exists(src):
                            os.replace(src, f"{self._path}.{i + 1}")
                    os.replace(self._path, f"{self._path}.1")
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass  # el registro de accesos nunca debe tumbar la API
