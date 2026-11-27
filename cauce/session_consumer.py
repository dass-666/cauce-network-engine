"""Consumidor de REFERENCIA del contrato de sesiones para Cauce Forms (E5). Solo diagnóstico.

No es la implementación de Forms (esa es de su autor y no se toca): es el comportamiento que el contrato exige,
escrito una vez y probado contra el motor real (tests/test_contrato_forms.py). Forms puede copiarlo, usarlo o
reimplementarlo; las pruebas de contrato dicen qué debe cumplir cualquier versión.

Reglas del contrato que este código hace cumplir:
1. Solo se envía estado: ``status`` ok/error, ``error_code`` de la lista cerrada y ``operation`` idle/in_progress.
   Nunca URL, cookies, campos de formulario, contenido de páginas ni datos personales.
2. Network Engine es OBSERVADOR: si no responde, el estado es «sin información» (nunca «sin internet») y la acción
   de la persona NO se bloquea ni se retrasa.
3. Ninguna acción se repite sola. ``operacion()`` envuelve una acción que la persona inició y la ejecuta UNA vez;
   si algo falla a medias, el resultado es «revisar a mano», no un reintento.
4. ``requires_human`` solo lo quita una persona con credencial admin (``resolve``). El consumidor no tiene forma de
   quitarlo y no lo intenta.
5. Tras un reinicio del motor: si conserva la sesión, se sigue latiendo con el mismo id; si respondió 404, se registra
   de nuevo; si el propio Forms perdió el id, el nuevo registro sustituye la sesión restaurada y HEREDA la marca.
6. 401: la credencial no vale; se deja de latir y se avisa. 429: se espera, sin insistir.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from .client import CauceAPIError, CauceSessionClient

# Estados que ve la persona en Forms
SIN_INFORMACION = "sin_informacion"          # el motor no responde: no se sabe nada de la red
CREDENCIAL_INVALIDA = "credencial_invalida"
DEMASIADO_FRECUENTE = "demasiado_frecuente"


@dataclass
class EstadoVisible:
    estado: str                               # fase del motor (activa, incierta, …) o uno de los de arriba
    requiere_persona: bool = False
    motivo: Optional[str] = None
    detalle: dict = field(default_factory=dict)

    @property
    def puede_reintentar_automaticamente(self) -> bool:
        return False                          # por contrato, nunca


class SesionDiagnostico:
    def __init__(self, cliente: CauceSessionClient, etiqueta: str, conexion: Optional[str] = None,
                 latido_max_s: float = 30.0, reloj: Callable[[], float] = time.monotonic):
        self.c = cliente
        self.etiqueta = etiqueta
        self.conexion = conexion
        self.latido_max_s = latido_max_s
        self.reloj = reloj
        self.detenida = False
        self._espera_hasta = 0.0
        self.ultimo = EstadoVisible(SIN_INFORMACION)
        self.marcas_no_enviadas = 0           # veces que no se pudo avisar de una operación (para mostrarlo)

    # ------------------------------------------------------------------ ciclo de vida
    def iniciar(self) -> EstadoVisible:
        if self.c.session_id:                 # Forms recordaba su id: primero se intenta seguir con él
            return self.latir()
        return self._registrar()

    def _registrar(self) -> EstadoVisible:
        try:
            v = self.c.register(self.etiqueta, connection_id=self.conexion, heartbeat_timeout_s=self.latido_max_s)
        except CauceAPIError as e:
            return self._error_api(e)
        except OSError:
            return self._fijar(EstadoVisible(SIN_INFORMACION))
        return self._desde_vista(v)

    def latir(self, ok: bool = True, error_code: Optional[str] = None, operation: Optional[str] = None) -> EstadoVisible:
        if self.detenida:
            return self.ultimo
        if self.reloj() < self._espera_hasta:
            return self.ultimo
        if not self.c.session_id:
            return self._registrar()
        try:
            v = self.c.heartbeat(ok, error_code, operation)
        except CauceAPIError as e:
            if e.status == 404:               # el motor ya no conoce la sesión: registrarla otra vez
                self.c.session_id = None
                return self._registrar()
            return self._error_api(e)
        except OSError:
            return self._fijar(EstadoVisible(SIN_INFORMACION))
        return self._desde_vista(v)

    def cerrar(self) -> None:
        if self.c.session_id and not self.detenida:
            with contextlib.suppress(CauceAPIError, OSError):
                self.c.close()

    # ------------------------------------------------------------------ operación de la persona
    @contextlib.contextmanager
    def operacion(self) -> Iterator["SesionDiagnostico"]:
        """Envuelve UNA acción que inició la persona (p. ej. pulsar Enviar). No la repite nunca.

        Avisa «en curso» antes y «idle» después. Si el motor no responde, la acción sigue igual (el motor solo
        observa) y se cuenta en ``marcas_no_enviadas`` para que la interfaz lo diga.
        """
        antes = self.latir(operation="in_progress")
        if antes.estado == SIN_INFORMACION:
            self.marcas_no_enviadas += 1
        try:
            yield self
        finally:
            # se espera lo mínimo para no chocar con el límite de frecuencia del motor
            time.sleep(0.55)
            despues = self.latir(operation="idle")
            if despues.estado == SIN_INFORMACION:
                self.marcas_no_enviadas += 1

    # ------------------------------------------------------------------ interpretación
    def _desde_vista(self, v: dict) -> EstadoVisible:
        return self._fijar(EstadoVisible(v.get("phase") or "incierta", bool(v.get("requires_human")),
                                         v.get("human_reason"), {"state": v.get("state"), "reason": v.get("reason"),
                                                                 "restored": v.get("restored", False)}))

    def _error_api(self, e: CauceAPIError) -> EstadoVisible:
        if e.status == 401:
            self.detenida = True
            return self._fijar(EstadoVisible(CREDENCIAL_INVALIDA, motivo=e.code))
        if e.status == 429:
            self._espera_hasta = self.reloj() + 2.0
            return self._fijar(EstadoVisible(DEMASIADO_FRECUENTE, self.ultimo.requiere_persona, e.code))
        return self._fijar(EstadoVisible(SIN_INFORMACION, self.ultimo.requiere_persona, e.code))

    def _fijar(self, e: EstadoVisible) -> EstadoVisible:
        # una marca de persona ya vista no se «olvida» en la interfaz porque el motor deje de responder
        if e.estado in (SIN_INFORMACION, DEMASIADO_FRECUENTE) and self.ultimo.requiere_persona:
            e.requiere_persona = True
            e.motivo = e.motivo or self.ultimo.motivo
        self.ultimo = e
        return e


def campos_enviados_permitidos() -> dict[str, Any]:
    """Lo único que el contrato deja enviar (para pruebas y para revisar implementaciones)."""
    return {"register": {"label", "connection_id", "authorized_by_user", "heartbeat_timeout_s"},
            "heartbeat": {"status", "error_code", "operation"}}
