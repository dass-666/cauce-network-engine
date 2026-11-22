# Guía para consumir Network Engine desde Cauce Monitor (solo lectura)

> **Estado: guía + cliente de referencia validados en SIMULACIÓN (loopback, datos ficticios).** No se ha probado con Cauce Monitor real,
> ni en Windows físico, ni con dos proveedores de internet. No se programó nada en el repositorio de Monitor: el código de
> `examples/consumidor_lectura.py` es un modelo para copiar. Evidencia: `docs/evidencia/informe-validacion-consumidor.md`.
> Referencias: `docs/LECTURA_PROTEGIDA.md` (diseño) y `docs/CONTRATO_SOLO_LECTURA.md` (contrato). Si difieren, manda `LECTURA_PROTEGIDA.md`.

## 0. Regla de oro

> Rama `feat/sesiones-navegador`: se añade `GET /api/v1/status/sessions` (estado de las sesiones de navegador). Contrato en
> [SESIONES_NAVEGADOR.md](SESIONES_NAVEGADOR.md) §4. Lo demás de esta guía no cambia.

Monitor **solo** puede usar `GET /api/v1/status/connectivity` y `GET /api/v1/status/events`, con una credencial `read` propia.
No usar nunca el token de administración (`api.token`), ni `/connections`, `/events`, `/diagnostics`, `/routes`, etc. (devuelven 403 y contienen MAC/IP).
La pregunta que responde esta vista es: *«¿tiene este equipo alguna conexión utilizable según Network Engine?»*. No sirve para
saber si un servicio externo funciona.

## 1. Credencial (la hace el operador, una vez)

```
python -m cauce token create --name monitor --out monitor.token    # no muestra el token en consola
python -m cauce token list
python -m cauce token revoke <id>                                   # efecto inmediato
```

- El archivo `monitor.token` queda protegido con DPAPI **del usuario de Windows que lo creó**: Monitor debe correr con ese usuario.
- Monitor guarda **solo la ruta** del archivo en su configuración (nunca el texto del token, nunca `api.token`).
- Puerto: `8765` por defecto (si el motor usa otro, está en `api.json` del directorio de datos). Solo `127.0.0.1`.
- Se envía como `Authorization: Bearer <token>`. El cliente `CauceReadClient.from_token_file(ruta, port=...)` lo hace por ti.

## 2. Esquemas reales (salida del simulador)

`GET /api/v1/status/connectivity`:
```json
{"schema":"cauce.connectivity/1","api":"v1",
 "engine":{"version":"0.1.0","mode":"simulated","epoch":"9455ae95"},
 "generated_at":"2026-11-21T01:16:29Z","last_seq":6,
 "state":"usable","usable":true,"reason":null,
 "counts":{"connections":3,"available":2,"measured":2},
 "connections":[{"id":"sim-a","kind":"ethernet","link":"up","available":true,"measured":true,
                 "health":"healthy","availability_pct":100.0,"avg_latency_ms":12.0}]}
```
`GET /api/v1/status/events?since=0&limit=3` (la respuesta completa; `limit` máx. 500):
```json
{"schema":"cauce.status-events/1","api":"v1","epoch":"9455ae95","last_seq":7,
 "gap":false,"restarted":false,"resync_required":false,"has_more":true,"next_since":3,
 "events":[{"seq":1,"ts":"2026-11-21T01:16:29Z","kind":"inventory.added","connection_id":"sim-a"}]}
```
Tipos de evento: `health.change` (`old`,`new`), `health.recovered`, `inventory.added|changed|removed`, `engine.start|stop`. Campos de nivel
superior exactos fijados por prueba (`test_field_whitelist_is_exact`): un campo nuevo no aparece sin cambiar esa prueba.

## 3. Cómo interpretar `state` / `usable`

| `state` | `usable` | Qué hacer en Monitor |
|---|---|---|
| `usable` | `true` | Todo normal. |
| `degraded` | `true` | Hay conexión pero lenta/inestable: aviso opcional, no bloquear nada. |
| `unavailable` | `false` | Único caso afirmativo de «sin conexión utilizable» (hay mediciones o enlaces caídos). |
| `unknown` | **`null`** | **«Aún no sé».** Ocurre al arrancar el motor o aparecer una conexión. **No** es fallo: Monitor sigue como si la integración no existiera y vuelve a preguntar. |

Regla: **`null` nunca se convierte en `false`.** Tampoco «el motor no responde»: eso es «sin información»
(`usable=None`), no «sin internet». Prueba: `test_unknown_is_null_and_does_not_become_false`, `test_engine_unreachable_is_not_no_internet`.

## 4. Cliente de ejemplo

Ver `examples/consumidor_lectura.py` (≈100 líneas, solo biblioteca estándar + `cauce/client.py`). Esencia:

```python
from consumidor_lectura import StatusConsumer
from cauce.client import CauceReadClient

c = StatusConsumer(CauceReadClient.from_token_file("monitor.token"), interval_s=10, kill_file="monitor-cauce.off")
while True:
    r = c.poll()                 # {"state","usable","detail"}; nunca lanza excepciones de red
    if r["usable"] is False:     # SOLO esto indica «sin conexión utilizable»
        ...                      # decisión de Monitor
    time.sleep(c.next_delay())   # incluye espera creciente si hay fallos
```
Para **solo conocer el estado actual basta `/status/connectivity`** (es lo más simple y detecta también reinicios por `engine.epoch`).
`follow_events=True` solo hace falta si Monitor quiere historial de cambios.

## 5. Frecuencia

- Cada **10 s** (mínimo absoluto 5 s: el ejemplo lo impone). El motor solo lee memoria: la consulta no sondea ni detecta nada (probado).
- Respuesta < 1 KB con 3 conexiones. Timeout del cliente: 2 s.
- Con errores: espera 2×, 4×, 8×… el intervalo, tope 60 s; al primer éxito vuelve al intervalo normal.
- No hay límite de peticiones en el servidor (limitación conocida): la disciplina es del consumidor.

## 6. Manejo de errores

| Respuesta | Causa | Acción en Monitor |
|---|---|---|
| Conexión rechazada / timeout / JSON ilegible | Motor apagado o bloqueado | `usable=None` («sin información»), espera creciente, **seguir reintentando**. |
| `503 credentials_locked` | El motor no pudo guardar una revocación y bloqueó las credenciales read | Igual que arriba (`usable=None`). Lo arregla el operador (`cauce token recover`). |
| `401 unauthorized` / `token_revoked` | Token inexistente o revocado | **Dejar de consultar** (reintentar no sirve), `usable=None`, registrar el *código* una vez, avisar al operador. |
| `403 insufficient_scope` | Se llamó una ruta no permitida (bug del consumidor) | Dejar de consultar y corregir el código. |
| `400 unsupported_parameter` | Se añadió un parámetro (p. ej. `refresh=1`) | Quitarlo: la vista no admite ninguno. |

Nota técnica: `CauceReadClient.connectivity_or_unknown()` quedó corregido en el PR #7. Ahora convierte en «sin información» (`state: engine_unreachable`, `usable: null`)
el motor apagado y los errores 5xx (p. ej. `503 credentials_locked`); **401, 403 y 400 se propagan como `CauceAPIError`** y dejan el cliente detenido (`halted`):
las llamadas siguientes repiten el error sin enviar peticiones hasta que el operador corrija la credencial y llame **explícitamente** a `reset()`
(nunca desde un bucle de reintentos). El ejemplo `StatusConsumer.poll` aplica la misma clasificación (pruebas de compatibilidad en `tests/test_client_errors.py`).

## 7. Reinicios del motor y pérdida de eventos

- Cada arranque del motor genera un `engine.epoch` nuevo (visible en ambas respuestas) y reinicia `last_seq`.
- En `/status/connectivity`: si `engine.epoch` cambia **o** `last_seq` retrocede → el motor se reinició: descartar estado anterior, usar el nuevo y fijar `since = last_seq`.
- En `/status/events`: si `resync_required` es `true` (`gap`: se perdieron eventos; `restarted`: `since` > `last_seq`) o el `epoch` cambió → releer `/status/connectivity` y empezar desde su `last_seq`.
- Mientras `has_more` sea `true`, repetir con `since = next_since` sin esperar.
- Los saltos de `seq` entre eventos devueltos son normales (hay eventos no visibles para `read`).
- Las credenciales `read` **sobreviven** al reinicio del motor; el token de sesión admin no (no es asunto de Monitor).
- Probado: reinicio con `seq` mayor que el anterior (solo lo delata el `epoch`), pérdida de eventos, motor apagado y vuelta.

## 8. No registrar MAC ni IP

La vista reducida **no contiene** MAC, IP pública/locales, gateway, nombres de adaptador ni datos de otros sistemas (lista blanca, probado con datos
del simulador). Aun así, en Monitor:
- No volcar la respuesta completa a logs ni a la base de datos; registrar `state`, `usable`, `reason` y contadores (`counts`).
- No registrar el texto de excepciones de red (pueden incluir host:puerto); registrar el tipo (`URLError`) o el `code` de la API.
- No registrar nunca el token ni su ruta con contenido. El `id` de conexión (`win-14`) identifica un adaptador, no una persona; evitarlo en logs salvo diagnóstico.
- El motor guarda un `access.jsonl` con nombre de consumidor, método, ruta, estado y **solo los nombres** de parámetros: sin token ni valores.

## 9. Desactivar la integración sin afectar a Monitor

1. **Interruptor sin tocar código**: variable de entorno `CAUCE_STATUS_DISABLED=1` o crear el archivo configurado (`kill_file`). Efecto probado: cero peticiones, resultado `disabled`, sin esperas ni errores, aunque el motor esté apagado.
2. **Desde Network Engine**: `cauce token revoke <id>` → el consumidor recibe `401 token_revoked` y se detiene solo.
3. **Diseño recomendado en Monitor**: la integración es un módulo opcional que solo *lee*; si devuelve `usable=None` o está desactivada, Monitor se comporta exactamente como hoy. Network Engine nunca debe ser requisito para arrancar Monitor.
4. Desinstalar: borrar la configuración de la ruta del token y revocar la credencial.

## 10. Seguridad: qué puede y qué no puede un consumidor `read`

No puede (todo da `403 insufficient_scope` y **se comprueba que no ocurrió nada**): crear rutas, seleccionar conexiones, crear/activar rotaciones,
ejecutar o detener tareas, iniciar sondeos, refrescar interfaces, detener el motor, listar/crear/revocar credenciales (incluida una `admin`: ni siquiera
existe ese permiso para credenciales), ni leer `/connections`, `/diagnostics`, `/events`.
Tampoco puede alterar el simulador. Recorrido automático de **todas** las rutas registradas: una ruta nueva nace como `admin`.

**Límites que debes conocer** (no se resuelven con esta guía):
- **DPAPI no aísla procesos del mismo usuario de Windows.** Cualquier proceso que corra con tu usuario puede descifrar tanto `monitor.token` como `api.token` (administración).
  El token `read` protege contra errores y fugas **del consumidor**, no contra malware con tu mismo usuario. Aislar de verdad exigiría otro usuario o un servicio con ACL (no implementado).
- Sin límite de peticiones por credencial; HTTP sin TLS en loopback; revocación sin persistencia si el disco falla (503 + `cauce token recover`).
- Sin probar en hardware ni con Monitor real; la conmutación entre dos proveedores sigue sin probarse físicamente.

## 11. Checklist para el Claude de Monitor

- [ ] Solo `read`; ruta del token en config, nunca `api.token`.
- [ ] Solo las dos rutas `/status/*`, sin parámetros extra.
- [ ] `null` ≠ `false`; error de red/503 ⇒ «sin información».
- [ ] 401/403/400 ⇒ detenerse y avisar (una vez).
- [ ] Intervalo ≥ 5 s (recomendado 10 s) con espera creciente.
- [ ] Reinicio por `epoch`/`last_seq`; `resync_required` ⇒ releer estado.
- [ ] Logs sin respuesta completa, sin IP/MAC/token.
- [ ] Interruptor de apagado y módulo opcional.
- [ ] Prueba propia de Monitor contra `python -m cauce sim` antes de tocar nada real.
