"""Almacenamiento protegido de secretos locales.

El motor no guarda contraseñas de redes ni credenciales de terceros. El único
secreto que maneja es el token de sesión de la API local, que se regenera en
cada arranque. Se guarda así:

- Windows: cifrado con DPAPI (CryptProtectData, ámbito del usuario actual).
  Solo el mismo usuario de Windows puede descifrarlo.
- Otros sistemas (solo desarrollo): archivo con permisos 0600. NO está
  cifrado; se documenta como limitación.
"""
from __future__ import annotations

import os
import sys

_MAGIC_DPAPI = b"CAUCE-DPAPI1\n"
_MAGIC_PLAIN = b"CAUCE-PLAIN1\n"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(blob_in), "cauce", None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def write_secret(path: str, value: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = value.encode("utf-8")
    if sys.platform == "win32":
        payload, method = _MAGIC_DPAPI + _dpapi(data, True), "dpapi"
    else:
        payload, method = _MAGIC_PLAIN + data, "file-0600"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    return method


def read_secret(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(_MAGIC_DPAPI):
        if sys.platform != "win32":
            raise OSError("Secreto cifrado con DPAPI: solo legible en Windows por el mismo usuario")
        return _dpapi(raw[len(_MAGIC_DPAPI):], False).decode("utf-8")
    if raw.startswith(_MAGIC_PLAIN):
        return raw[len(_MAGIC_PLAIN):].decode("utf-8")
    raise OSError("Formato de secreto desconocido")
