"""Consumidor de REFERENCIA de la vista de solo lectura (credencial ``read``).

NO es código de Cauce Monitor ni se ha integrado con él: es el modelo que Monitor puede copiar/adaptar
en su propio repositorio (ver docs/GUIA_CONSUMIDOR_MONITOR.md). Solo biblioteca estándar + ``CauceReadClient``.

Reglas que implementa (todas con prueba en tests/test_consumer_validation.py):
- Solo usa /status/connectivity y /status/events con un token ``read``; nunca la credencial administrativa.
- «Sin información» (``usable`` None) NO es «sin internet»: ante cualquier fallo el resultado es ``usable=None``.
- Detecta reinicios del motor (cambio de ``epoch`` o ``last_seq`` que retrocede) y pérdida de eventos (``gap``).
- Credencial rechazada (401/403/400): se DETIENE (reintentar solo satura); fallo transitorio (503, motor apagado): espera creciente.
- Nunca registra respuestas completas, tokens, IP ni MAC: solo estado, ``usable`` y códigos.
- Interruptor de apagado: variable CAUCE_STATUS_DISABLED=1 o un archivo; desactivado = cero peticiones.

Uso mínimo (simulación):  python -m cauce sim   y   python -m cauce token create --name monitor --out monitor.token
    python examples/consumidor_lectura.py monitor.token
"""
import logging
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cauce.client import CauceAPIError, CauceReadClient  # noqa: E402

log = logging.getLogger("cauce.consumer")
MIN_INTERVAL_S = 5.0     # nunca consultar con más frecuencia
MAX_BACKOFF_S = 60.0
KILL_ENV = "CAUCE_STATUS_DISABLED"
_HALT_CODES = {401, 403, 400}   # no se arreglan reintentando: credencial inválida/revocada, permiso o petición mal hecha


class StatusConsumer:
    def __init__(self, client: CauceReadClient, interval_s: float = 10.0, kill_file: Optional[str] = None,
                 follow_events: bool = False):
        self.client = client
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s))
        self.kill_file = kill_file
        self.follow_events = follow_events
        self.epoch: Optional[str] = None
        self.last_seq = 0
        self.since = 0
        self.failures = 0
        self.halted: Optional[str] = None
        self.restarts = 0
        self.resyncs = 0
        self.result = {"state": "no_info", "usable": None, "detail": "not_polled"}

    # --- control -------------------------------------------------------------------------
    def disabled(self) -> bool:
        return os.environ.get(KILL_ENV) == "1" or bool(self.kill_file and os.path.exists(self.kill_file))

    def next_delay(self) -> float:
        return self.interval_s if not self.failures else min(MAX_BACKOFF_S, self.interval_s * 2 ** self.failures)

    def _set(self, state, usable, detail=None):
        new = {"state": state, "usable": usable, "detail": detail}
        if (new["state"], new["usable"], new["detail"]) != (self.result["state"], self.result["usable"], self.result["detail"]):
            log.info("conectividad: estado=%s usable=%s detalle=%s", state, usable, detail)   # nada más
        self.result = new
        return new

    # --- una vuelta ----------------------------------------------------------------------
    def poll(self) -> dict:
        """Devuelve {state, usable, detail}. ``usable`` es True/False solo si el motor lo afirmó; si no, None."""
        if self.disabled():
            return self._set("disabled", None, "kill_switch")        # cero peticiones
        if self.halted:
            return self._set("no_info", None, self.halted)           # cero peticiones hasta que un humano intervenga
        try:
            v = self.client.connectivity()
            self._check_restart(v["engine"]["epoch"], v["last_seq"])
            if self.follow_events:
                self._drain_events()
            self.failures = 0
            return self._set(v["state"], v["usable"], v.get("reason"))
        except CauceAPIError as e:
            if e.status in _HALT_CODES:
                self.halted = e.code
                log.error("credencial/petición rechazada (%s): integración detenida hasta intervención del operador", e.code)
            else:                                                     # 503 credentials_locked, 5xx…
                self.failures += 1
                log.warning("motor respondió error transitorio (%s)", e.code)
            return self._set("no_info", None, e.code)
        except (OSError, ValueError) as e:                            # apagado, tiempo agotado, JSON ilegible
            self.failures += 1
            log.warning("motor no responde (%s)", type(e).__name__)   # tipo, no el texto del error
            return self._set("no_info", None, "engine_unreachable")

    def _check_restart(self, epoch: str, last_seq: int) -> None:
        if self.epoch is not None and (epoch != self.epoch or last_seq < self.last_seq):
            self.restarts += 1
            self.since = last_seq                                     # el contador empezó de nuevo
            log.warning("motor reiniciado; estado releído")
        elif self.epoch is None:
            self.since = last_seq
        self.epoch, self.last_seq = epoch, last_seq

    def _drain_events(self) -> None:
        while True:
            ev = self.client.events(self.since)
            if ev["epoch"] != self.epoch or ev["resync_required"]:
                self.resyncs += 1
                self.epoch, self.since = ev["epoch"], ev["last_seq"]  # el estado ya se acaba de leer arriba
                log.warning("eventos perdidos o motor reiniciado; se usa el estado releído")
                return
            self.since = ev["next_since"]
            if not ev["has_more"]:
                return


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    c = StatusConsumer(CauceReadClient.from_token_file(argv[1]), follow_events=True)
    while True:
        c.poll()
        time.sleep(c.next_delay())


if __name__ == "__main__":
    main(sys.argv)
