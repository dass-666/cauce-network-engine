"""Persistencia local mínima del ESTADO de diagnóstico de las sesiones de navegador (E5).

Qué se guarda (lista blanca, sin secretos): id y etiqueta de la sesión, nombre e id de la credencial dueña (el id, nunca el
token), conexión de la que depende, tiempo de espera de latidos, último estado y fase, si había una operación en curso, si
requiere intervención humana y por qué, y el historial de las últimas sesiones cerradas. No se guardan URL, cookies,
formularios, contenido de páginas ni datos personales: el motor nunca los recibe.

Cómo se escribe: JSON en un temporal, fsync y `os.replace` (un lector nunca ve un archivo a medias). Permisos: 0600 en
Linux/macOS; en Windows los da la ACL de la carpeta de datos del usuario (%LOCALAPPDATA%), igual que audit.jsonl. Antes de reemplazar,
la versión buena anterior pasa a `.bak`. El contenido lleva un resumen SHA-256: una escritura cortada o un archivo
alterado se detecta al cargar. Si el principal está dañado se intenta el `.bak`; si también lo está, el archivo dañado se
aparta (`.corrupt-<fecha>`) y se empieza vacío, y la auditoría lo anota para que una persona lo revise.

Reversible: `sessions.persist: false` en la configuración la apaga; borrar el archivo con el motor parado la reinicia.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Optional

SCHEMA = "cauce.sessions-state/1"
MAX_BYTES = 1_000_000


def _digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _replace(src: str, dst: str, intentos: int = 20) -> None:
    """os.replace con reintentos: en Windows falla con PermissionError mientras otro proceso tiene abierto el destino
    (un lector o un escritor simultáneo). Encontrado en la CI de Windows (E5)."""
    for i in range(intentos):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == intentos - 1:
                raise
            time.sleep(0.02 * (i + 1))


class SessionStore:
    def __init__(self, path: str):
        self.path = path
        self.bak = path + ".bak"
        self._lock = threading.Lock()          # dentro de un proceso, una escritura cada vez

    # ------------------------------------------------------------------ escritura
    def save(self, sessions: list[dict], closed: list[dict], saved_at: float) -> None:
        """Lanza OSError si no se pudo escribir (el llamador lo anota; el motor sigue funcionando)."""
        with self._lock:
            self._save(sessions, closed, saved_at)

    def _save(self, sessions: list[dict], closed: list[dict], saved_at: float) -> None:
        body = {"schema": SCHEMA, "saved_at": saved_at, "sessions": sessions, "closed": closed}
        doc = {**body, "sha256": _digest(body)}
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"   # único: dos escritores no comparten temporal
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            if os.path.exists(self.path) and self._read(self.path) is not None:
                try:
                    _replace(self.path, self.bak)        # la versión buena anterior queda de respaldo
                except FileNotFoundError:
                    pass                                 # otro escritor la movió entre medias: su respaldo vale igual
            _replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------ lectura
    @staticmethod
    def _read(path: str) -> Optional[dict]:
        try:
            if os.path.getsize(path) > MAX_BYTES:
                return None
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            return None
        body = {k: doc.get(k) for k in ("schema", "saved_at", "sessions", "closed")}
        if doc.get("sha256") != _digest(body):
            return None
        if not isinstance(body["sessions"], list) or not isinstance(body["closed"], list):
            return None
        if not isinstance(body["saved_at"], (int, float)):
            return None
        return body

    def load(self) -> tuple[Optional[dict], str]:
        """(contenido, estado). estado: missing | ok | backup | corrupt."""
        if not os.path.exists(self.path) and not os.path.exists(self.bak):
            return None, "missing"
        doc = self._read(self.path) if os.path.exists(self.path) else None
        if doc is not None:
            return doc, "ok"
        doc = self._read(self.bak) if os.path.exists(self.bak) else None
        if os.path.exists(self.path):
            # se aparta el archivo dañado para que una persona pueda revisarlo; nunca se borra
            try:
                os.replace(self.path, f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            except OSError:
                pass
        if doc is not None:
            return doc, "backup"
        return None, "corrupt"


def clean_session(d: Any) -> Optional[dict]:
    """Valida una sesión leída del disco (lista blanca de campos y tipos). None si no es válida."""
    if not isinstance(d, dict):
        return None
    str_or_none = lambda v: v if v is None or isinstance(v, str) else False  # noqa: E731
    out = {
        "id": d.get("id"), "label": d.get("label"), "owner_token_id": d.get("owner_token_id"),
        "owner_name": d.get("owner_name"), "connection_id": str_or_none(d.get("connection_id")),
        "heartbeat_timeout_s": d.get("heartbeat_timeout_s"), "registered_at": d.get("registered_at"),
        "last_heartbeat": d.get("last_heartbeat"), "state": d.get("state"), "operation": str_or_none(d.get("operation")),
        "requires_human": d.get("requires_human") is True, "human_reason": str_or_none(d.get("human_reason")),
        "heartbeats": d.get("heartbeats") if isinstance(d.get("heartbeats"), int) else 0,
    }
    if not all(isinstance(out[k], str) and out[k] for k in ("id", "label", "owner_token_id", "owner_name", "state")):
        return None
    if False in (out["connection_id"], out["operation"], out["human_reason"]):
        return None
    if not isinstance(out["heartbeat_timeout_s"], (int, float)) or not isinstance(out["registered_at"], (int, float)):
        return None
    if out["last_heartbeat"] is not None and not isinstance(out["last_heartbeat"], (int, float)):
        return None
    return out
