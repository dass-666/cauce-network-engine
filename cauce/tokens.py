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
CREATABLE_SCOPES = (SCOPE_READ,)  # un token admin nunca se crea por la API
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
    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._lock = threading.Lock()
        self._tokens: dict[str, dict[str, Any]] = {}
        self._usage: dict[str, dict[str, Any]] = {}   # solo en memoria
        if path and os.path.exists(path):
            self._load()

    # --- persistencia ---------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(read_secret(self._path))
            self._tokens = {t["id"]: t for t in data.get("tokens", [])}
        except (OSError, ValueError, KeyError):
            # Registro ilegible (p. ej. de otro usuario): no se adivina nada, ningún token read es válido.
            self._tokens = {}

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        write_secret(tmp, json.dumps({"version": 1, "tokens": list(self._tokens.values())}))
        os.replace(tmp, self._path)

    # --- administración -------------------------------------------------
    def create(self, name: str, scope: str = SCOPE_READ) -> tuple[dict[str, Any], str]:
        if scope not in CREATABLE_SCOPES:
            raise TokenError(400, "invalid_scope", f"Permiso no permitido: {scope!r} (solo {', '.join(CREATABLE_SCOPES)})")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise TokenError(400, "invalid_name", "Nombre inválido: 2-32 caracteres, minúsculas, dígitos, '-' o '_'")
        with self._lock:
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
            self._save()
        return self._public(rec), f"cr1.{tid}.{secret}"

    def revoke(self, token_id: str) -> dict[str, Any]:
        with self._lock:
            rec = self._tokens.get(token_id)
            if not rec:
                raise TokenError(404, "unknown_token", "Credencial inexistente")
            if not rec.get("revoked_at"):
                rec["revoked_at"] = round(time.time(), 3)
                self._save()
            return self._public(rec)

    def _public(self, rec: dict[str, Any]) -> dict[str, Any]:
        u = self._usage.get(rec["id"], {})
        return {"id": rec["id"], "name": rec["name"], "scope": rec["scope"], "created_at": rec["created_at"],
                "revoked_at": rec.get("revoked_at"), "last_used": u.get("last_used"), "requests": u.get("requests", 0)}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(t) for t in self._tokens.values()]

    # --- verificación ---------------------------------------------------
    def verify(self, presented: str) -> Principal:
        """Devuelve el Principal o lanza TokenError 401. Nunca registra ``presented``."""
        m = _TOKEN_RE.match(presented or "")
        if not m:
            raise TokenError(401, "unauthorized", "Falta token válido (Authorization: Bearer ...)")
        tid, secret = m.groups()
        with self._lock:
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
