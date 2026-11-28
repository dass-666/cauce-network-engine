# Contrato de sesiones para Cauce Forms (diagnóstico, E5)

**Estado:** propuesta probada contra Network Engine, **no integrada en Forms**. Forms pertenece a su autor y no se ha
modificado. El PR #8 sigue abierto. Este documento, `cauce/session_consumer.py` y `tests/test_contrato_forms.py`
fijan qué debe cumplir cualquier implementación.

## Qué es
Forms informa a Network Engine del **estado** de cada sesión de navegador que la persona abrió y autorizó. Network
Engine lo combina con lo que mide de la red, y Monitor lo muestra. **Nadie decide por la persona.**

## Qué NO es
- Network Engine no abre, controla ni encamina el navegador. El navegador sigue la ruta de Windows.
- No se envían URL, cookies, campos, contenido de páginas ni datos personales.
- No se repite ni se reenvía ninguna acción.
- No se rota la IP, no se cambia de conexión por visita y no se esquiva ninguna restricción de terceros.

## Credenciales
Una credencial `session` por sesión, creada por el administrador (`cauce token create forms-a --scope session`) y
guardada protegida con DPAPI. Forms no recibe ni necesita la credencial admin.

## Llamadas (API local `http://127.0.0.1:<puerto>/api/v1`)
| Momento en Forms | Llamada | Cuerpo permitido |
|---|---|---|
| La persona abre la sesión | `POST /sessions` | `label`, `connection_id` (opcional), `authorized_by_user: true`, `heartbeat_timeout_s` |
| Cada 5–10 s mientras está abierta | `POST /sessions/{id}/heartbeat` | `status` ok/error, `error_code` (lista cerrada), `operation` |
| Justo **antes** de que la persona envíe | latido con `operation: "in_progress"` | — |
| Justo **después** (salga bien o mal) | latido con `operation: "idle"` | — |
| La persona cierra | `DELETE /sessions/{id}` | — |

Los latidos deben ir separados al menos 0,5 s. Por debajo, el motor responde 429.

## Respuestas y qué debe hacer Forms
| Respuesta | Significado | Forms debe |
|---|---|---|
| 200 con `phase` | estado calculado | mostrar la fase; si `requires_human`, mostrar «revisar a mano» con `human_reason` |
| sin respuesta, o error de conexión | **sin información** | seguir funcionando; no decir «sin internet»; no bloquear a la persona |
| 404 `unknown_session` | el motor ya no la conoce (caducada, cerrada o sin persistencia) | registrarla de nuevo |
| 409 `credential_has_session` | esa credencial ya tiene otra sesión viva | no crear otra; usar el id que ya tenía |
| 401 | credencial inválida o revocada | dejar de latir y avisar al administrador |
| 429 | latidos demasiado seguidos | esperar unos segundos; no insistir |

## Reglas que nunca se rompen
1. **Una operación a medias no se repite.** Si el motor marcó `requires_human`, la persona decide si el envío se
   completó, revisando el portal o el acuse. Ningún código reintenta por ella.
2. **`requires_human` solo lo quita una persona** con credencial admin, mediante `POST /sessions/{id}/resolve`. No lo
   quitan ni un latido, ni la recuperación de la red, ni un reinicio del motor o de Forms, ni un nuevo registro.
3. **Si el motor se reinicia**, la sesión vuelve como «incierta» (`restored: true`). Si había una operación en curso,
   vuelve con `requires_human` (`engine_restart_during_operation`).
4. **Si Forms también perdió el id**, un nuevo `POST /sessions` con la misma credencial sustituye la sesión restaurada y
   **hereda** la marca.
5. **Si la acción falla a medias** (excepción, cierre del navegador, tiempo agotado tras pulsar Enviar), Forms envía un
   latido `status: "error"` con `operation: "in_progress"` y **no** reintenta: el motor marca `requires_human`
   (`operation_interrupted`) y Forms muestra «revisar a mano» aunque el motor no responda (E6).
6. **«En curso» se envía siempre antes de actuar.** Si hubo un latido hace menos de 0,5 s, Forms espera ese resto; si no,
   el motor respondería 429 y la operación quedaría sin marcar (defecto demostrado en E6 en el consumidor de referencia).
7. **Network Engine solo observa.** Si no responde, Forms funciona igual. La acción de la persona nunca espera al motor.

## Qué demuestran las pruebas de contrato
`tests/test_contrato_forms.py` usa API real, red simulada y reinicio real del motor sobre la misma carpeta y el mismo
puerto. Comprueba:
- ciclo normal y lista blanca de campos;
- motor apagado durante el envío: la acción no se bloquea y el estado es «sin información»;
- reinicio del motor a mitad del envío: queda `requires_human` y no se repite nada;
- reinicio de Forms y del motor: la marca se hereda;
- sin persistencia, un 404 lleva a registrar de nuevo;
- 401 detiene los latidos;
- 429 no se insiste;
- la marca no se olvida si el motor cae.

`tests/test_contrato_forms_no_repeticion.py` (E6) añade: la acción se ejecuta una sola vez ante cualquier respuesta
del motor; una acción que falla a medias queda para revisar (motor y pantalla); «en curso» llega aunque haya un latido
justo antes; una credencial `session` no puede resolver la marca (403).

## Qué falta, y es decisión del autor de Forms
- Llamar a esto desde Forms, en su repositorio y con su revisión.
- Elegir dónde se muestra «revisar a mano» en su interfaz.
- Decidir si Forms recuerda el id de sesión entre reinicios. Es opcional: la regla 4 cubre el caso.

## E10: cambios del contrato para Forms nativo (rama `claude/e10-forms-nativo`, laboratorio)

Estos cambios son del laboratorio E10. RC2 y RC3 no los tienen. Todo lo anterior sigue valiendo.

### S1. Recuperar la sesión propia cuando Forms pierde su id
- `POST /sessions` con la MISMA credencial que ya tiene una sesión:
  - si la anterior sigue latiendo (silencio < `heartbeat_timeout_s`): **409 `credential_has_session`** con
    `error.details = {"retry_after_s": N, "silent_s": S}`. Nada se desplaza. Forms espera `retry_after_s` y reintenta;
  - si la anterior ya calló: la nueva la sustituye (`session.replaced`, motivo `replaced_by_owner`). La nueva
    **hereda** `requires_human` de la anterior; si la anterior tenía una operación «en curso», la nueva nace con
    `requires_human` y motivo `owner_replaced_during_operation`. No se inventa una marca si no la había.
- Otra credencial nunca sustituye una sesión ajena (sigue el 409 de siempre).
- Con seis registros simultáneos de la misma credencial gana exactamente uno (prueba de concurrencia).

### S2. La marca que reaparecía tras resolverla
- `POST /sessions/{id}/resolve` (admin) acepta `{"note": "..."}` (≤ 500 caracteres; en la auditoría queda entera y al
  dueño solo le llega su huella).
- Si la condición que creó la marca **continúa**, responde **409 `condition_persists`** con
  `error.details.condition` = `owner_local_review_pending` o `operation_still_reported_in_progress`, y la marca no se
  toca. Antes se quitaba y volvía al instante. Ninguna protección se debilita: solo se dice por qué no puede cerrarse.

### S3. Forms puede saber qué revisó el administrador
- Latido con el campo opcional `local_review`: `"none"` o `"pending"`. `"pending"` significa «tengo operaciones
  locales con resultado desconocido». Mientras sea `"pending"`, la sesión lleva `requires_human`
  (`owner_local_review_pending`) y el resolve responde `condition_persists`.
- `GET /sessions/{id}/reviews` (credencial dueña, `session`): las últimas 20 revisiones de marcas de esa credencial o
  etiqueta. Cada una trae `confirms_receipt: false`, `evidence: "none"` y `meaning: "network_or_session_review"`.
  **Una revisión administrativa NO es un acuse**: Forms no debe pasar ninguna operación incierta a otro estado por
  ella. Solo un acuse verificable del portal o la revisión en el propio puesto cierran una operación incierta.
- Las revisiones se guardan aparte (`sessions_reviews.json` + `.bak`, con sha256), así que volver a RC3 no rompe el
  estado. Un archivo dañado se descarta sin tumbar el motor.

### Revocar una credencial cierra su sesión
- Al revocar, la sesión de esa credencial se cierra (`credential_revoked`). Si tenía una operación «en curso», queda
  una marca **pendiente** `credential_revoked_during_operation` que solo un operador resuelve. Antes la sesión seguía
  viva una hora y la credencial nueva recibía 403 `not_session_owner`.

### Qué demuestran las pruebas
`tests/test_e10_forms_nativo.py` (20 pruebas) y `tests/test_e10_instalador.py` (5). Son pruebas con el motor real y
reloj simulado. No son pruebas físicas.
