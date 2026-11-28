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
import re
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
        self._written_seq = 0

    # ------------------------------------------------------------------ escritura
    def save(self, sessions: list[dict], closed: list[dict], saved_at: float, pending: Optional[list[dict]] = None,
             seq: Optional[int] = None) -> bool:
        """Lanza OSError si no se pudo escribir (el llamador lo anota; el motor sigue funcionando).
        ``pending``: marcas de intervención humana sin resolver de sesiones ya cerradas (E6).
        ``seq``: número de foto creciente; una foto más vieja que la última escrita se descarta (E7). False si se
        descartó."""
        with self._lock:
            if seq is not None:
                if seq <= self._written_seq:
                    return False
            self._save(sessions, closed, saved_at, pending or [])
            if seq is not None:
                self._written_seq = seq
            return True

    def _save(self, sessions: list[dict], closed: list[dict], saved_at: float, pending: list[dict]) -> None:
        body = {"schema": SCHEMA, "saved_at": saved_at, "sessions": sessions, "closed": closed, "pending": pending}
        doc = {**body, "sha256": _digest(body)}
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"   # único: dos escritores no comparten temporal
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            previo = self._bytes_validos(self.path)
            if previo is not None:
                # E8: la versión buena anterior se COPIA al respaldo (antes se movía). Al moverla, el principal no
                # existía durante un instante y una muerte brusca justo ahí arrancaba desde el respaldo, con una
                # revisión humana innecesaria de todas las sesiones (21 % de 150 muertes con guardado continuo).
                self._copiar_a_respaldo(previo)
            _replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _bytes_validos(self, path: str) -> Optional[bytes]:
        """El contenido del archivo si es un estado válido; None si no existe, no se puede leer o está dañado.
        Se lee UNA vez y esos mismos bytes van al respaldo (en Windows, cada apertura extra puede chocar con el
        reemplazo de otro escritor)."""
        try:
            if os.path.getsize(path) > MAX_BYTES:
                return None
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        return raw if self._parse(raw) is not None else None

    def _copiar_a_respaldo(self, data: bytes) -> None:
        """Mejor esfuerzo y sin fsync: el respaldo es la versión ANTERIOR y su resumen se comprueba al leerlo. Solo el
        principal (la versión nueva) se fuerza a disco antes de responder. E8: en Windows cada fsync puede tardar más
        de 1 s (diagnostico-latido) y el cliente de sesión espera 2 s; dos fsync por latido eran demasiados."""
        btmp = f"{self.bak}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(btmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            _replace(btmp, self.bak)
        except OSError:
            pass                                         # sin respaldo nuevo queda el anterior; el principal no se toca
        finally:
            if os.path.exists(btmp):
                try:
                    os.remove(btmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------ lectura
    @staticmethod
    def _read(path: str) -> Optional[dict]:
        try:
            if os.path.getsize(path) > MAX_BYTES:
                return None
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        return SessionStore._parse(raw)

    @staticmethod
    def _parse(raw: bytes) -> Optional[dict]:
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            return None
        # "pending" se añadió en E6; un archivo anterior no lo trae y su resumen se calculó sin él
        keys = ("schema", "saved_at", "sessions", "closed") + (("pending",) if "pending" in doc else ())
        body = {k: doc.get(k) for k in keys}
        if doc.get("sha256") != _digest(body):
            return None
        body.setdefault("pending", [])
        if not all(isinstance(body[k], list) for k in ("sessions", "closed", "pending")):
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


def clean_pending(d: Any) -> Optional[dict]:
    """Valida una marca pendiente leída del disco. owner_token_id y label en None = marca general (afecta a todos)."""
    if not isinstance(d, dict):
        return None
    out = {k: d.get(k) for k in ("owner_token_id", "label", "reason", "session_id", "since")}
    pid = d.get("id")
    if not isinstance(out["reason"], str) or not out["reason"]:
        return None
    if any(out[k] is not None and not isinstance(out[k], str) for k in ("owner_token_id", "label", "session_id")):
        return None
    if not isinstance(out["since"], (int, float)):
        return None
    # E7: id inequívoco. Un archivo anterior no lo trae: se deriva de la marca, así es estable entre reinicios.
    if not (isinstance(pid, str) and PENDING_ID_RE.match(pid)):
        pid = pending_id_for(out)
    out["id"] = pid
    return out


PENDING_ID_RE = re.compile(r"^pm_[0-9a-f]{12}$")


def pending_id_for(p: dict) -> str:
    raw = json.dumps({k: p.get(k) for k in ("owner_token_id", "label", "reason", "session_id", "since")}, sort_keys=True)
    return "pm_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


REVIEWS_SCHEMA = "cauce.sessions-reviews/1"
REVIEW_ID_RE = re.compile(r"^rv_[0-9a-f]{12}$")


class ReviewStore:
    """E10 S3: resoluciones administrativas de marcas «revisar a mano», en un archivo APARTE de sessions_state.json.

    Aparte a propósito: RC3 no conoce este archivo y lo ignora, así que volver a RC3 no le hace ver su estado como dañado.
    Mismo método de escritura (temporal, fsync, reemplazo, resumen SHA-256, respaldo .bak). Lista blanca de campos; la nota
    del operador NO se guarda aquí (solo su huella): la nota completa queda en la auditoría.
    Si el archivo está dañado se usa el .bak; si tampoco vale, se empieza vacío (perder este registro no quita ninguna
    marca: solo deja de informar al dueño de revisiones pasadas)."""

    FIELDS = ("id", "kind", "owner_token_id", "label", "session_id", "pending_id", "reason", "resolved_at",
              "resolved_by", "note_sha256")

    def __init__(self, path: str):
        self.path = path
        self.bak = path + ".bak"
        self._lock = threading.Lock()
        self.status = "missing"

    @classmethod
    def _clean(cls, d: Any) -> Optional[dict]:
        if not isinstance(d, dict) or not REVIEW_ID_RE.match(str(d.get("id", ""))):
            return None
        out = {k: d.get(k) for k in cls.FIELDS}
        if out["kind"] not in ("session_resolve", "pending_resolve") or not isinstance(out["resolved_at"], (int, float)):
            return None
        if any(out[k] is not None and not isinstance(out[k], str)
               for k in ("owner_token_id", "label", "session_id", "pending_id", "reason", "resolved_by", "note_sha256")):
            return None
        return out

    def _read(self, path: str) -> Optional[list]:
        try:
            with open(path, "rb") as fh:
                raw = fh.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            doc = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != REVIEWS_SCHEMA or not isinstance(doc.get("reviews"), list):
            return None
        if doc.get("sha256") != _digest({"schema": REVIEWS_SCHEMA, "reviews": doc["reviews"]}):
            return None
        return [r for r in (self._clean(x) for x in doc["reviews"]) if r is not None]

    def load(self) -> list[dict]:
        if not os.path.exists(self.path) and not os.path.exists(self.bak):
            self.status = "missing"
            return []
        for path, status in ((self.path, "ok"), (self.bak, "backup")):
            if os.path.exists(path):
                r = self._read(path)
                if r is not None:
                    self.status = status
                    return r
        self.status = "corrupt"
        return []

    def save(self, reviews: list[dict]) -> None:
        body = {"schema": REVIEWS_SCHEMA, "reviews": [{k: r.get(k) for k in self.FIELDS} for r in reviews]}
        doc = {**body, "sha256": _digest(body)}
        with self._lock:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                if os.path.exists(self.path) and self._read(self.path) is not None:
                    with open(self.path, "rb") as src, open(self.bak + ".tmp", "wb") as dst:
                        dst.write(src.read())
                        dst.flush()
                        os.fsync(dst.fileno())
                    _replace(self.bak + ".tmp", self.bak)
                _replace(tmp, self.path)
            finally:
                for t in (tmp, self.bak + ".tmp"):
                    if os.path.exists(t):
                        try:
                            os.remove(t)
                        except OSError:
                            pass
