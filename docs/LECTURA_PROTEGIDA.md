# Lectura protegida: credenciales `read` y vista reducida de conectividad

> **Estado: implementado y probado en simulación y CI (Linux y Windows). NO probado con Monitor ni con otros
> consumidores reales, ni en hardware físico.** Complementa el contrato de la PR #4
> (`docs/CONTRATO_SOLO_LECTURA.md`): aquí se concreta la sección B4 y se *restringe*
> la B2 (ver «Diferencias con el contrato propuesto»). El contrato fue conciliado con esta implementación;
> si difieren, manda este documento. Orden de fusión recomendado: primero la PR #4 (documentación), luego la PR #5.

## 1. Qué se añadió

| Elemento | Detalle |
|---|---|
| Permisos | `admin` (token de sesión, como en v0.1.0) y `read` (credenciales por sistema). |
| Comprobación | En el servidor, **antes** de leer el cuerpo o ejecutar nada. Cada ruta declara su permiso; **el valor por defecto es `admin`**, así que una ruta nueva nunca queda abierta a `read` por olvido. |
| Falta de permiso | `403 insufficient_scope`. Token inexistente o mal formado: `401 unauthorized`. Token revocado: `401 token_revoked`. |
| Endpoints `read` | `GET /api/v1/status/connectivity` y `GET /api/v1/status/events` (más `GET /ping`, que ya era público). |
| Gestión (solo `admin`) | `POST /tokens`, `GET /tokens`, `DELETE /tokens/{id}` y la CLI `cauce token create\|list\|revoke`. |
| Registro de accesos | `access.jsonl` en el directorio de datos: hora, nombre del sistema, id de credencial, método, ruta, estado y código. Nunca el token, ni el cuerpo, ni los **valores** de la consulta (solo sus nombres). Rota a los 5 MB. |
| Auditoría | Se anotan `token.created` y `token.revoked` en la cadena con hashes. Las lecturas **no** se anotan ahí: llenarían la memoria de eventos y crearían huecos de secuencia a los consumidores. |

## 2. Cómo se usa (operador)

```
cauce sim                                            # o "cauce run"; el motor debe estar en marcha
cauce token create --name monitor --out monitor.token   # guarda el token protegido; no lo muestra
cauce token list
cauce token revoke <id>                              # efecto inmediato, sin reiniciar el motor; 503 si no pudo guardarse
cauce token recover                                  # solo si el almacén quedó bloqueado por un fallo de almacenamiento
```

- Un nombre por sistema (2-32 caracteres: minúsculas, dígitos, `-`, `_`). Máximo 20 credenciales activas.
- **No se puede crear una credencial `admin`**: solo existe el token de sesión, que nunca se entrega.
- El texto del token (`cr1.<id>.<secreto>`) se muestra una sola vez. El motor guarda solo su hash SHA‑256
  en `tokens.dat` (con DPAPI en Windows). Las credenciales **sobreviven** a reinicios; el token de sesión no.
- `--out` evita que el token pase por la consola o el historial. Es el camino recomendado.
- Consumidor (cliente de referencia, solo lectura): `CauceReadClient.from_token_file("monitor.token")`.

## 3. `GET /api/v1/status/connectivity`

```json
{
  "schema": "cauce.connectivity/1", "api": "v1",
  "engine": {"version": "0.1.0", "mode": "real", "epoch": "9f3a1c02"},
  "generated_at": "2026-11-20T21:00:00Z", "last_seq": 412,
  "state": "usable", "usable": true, "reason": null,
  "counts": {"connections": 2, "available": 1, "measured": 1},
  "connections": [
    {"id": "win-14", "kind": "wifi", "link": "up", "available": true, "measured": true,
     "health": "healthy", "availability_pct": 99.2, "avg_latency_ms": 23.5}
  ]
}
```

| `state` | `usable` | Significado |
|---|---|---|
| `usable` | `true` | Hay al menos una conexión disponible y sana. |
| `degraded` | `true` | Hay conexión disponible, pero todas con latencia alta o fallos parciales. |
| `unavailable` | `false` | Hay base para afirmarlo: ninguna conexión disponible (`reason`: `no_active_links` o `all_measured_connections_down`). |
| `unknown` | `null` | **Aún no hay mediciones suficientes** (`reason: insufficient_measurements`). Pasa tras arrancar el motor o al aparecer una conexión nueva. El consumidor debe seguir con su comportamiento normal. |

- `measured` es `false` mientras una conexión no tiene ningún sondeo resuelto; entonces `health` es `unknown` y las métricas son `null`.
- **No incluye**: MAC, IP pública, IPs locales, gateway, nombres de adaptador, grupo de salida, rutas (leases), tareas ni
  `owner`/`purpose` de otros sistemas. Se construye con **lista blanca** de campos: un campo nuevo del motor no sale hasta añadirlo a propósito.
- El `id` es el del motor (`win-<ifIndex>`, `lnx-<ifindex>`): identifica un adaptador dentro de este equipo, no a una persona.
- Parámetros de consulta: no admite ninguno. Cualquiera (p. ej. `refresh=1`) da `400 unsupported_parameter` y **no se ejecuta nada**.

### «El motor no responde» no es «no hay internet»

Este documento solo existe si Network Engine contesta. Si no contesta (parado, bloqueado, tiempo agotado, 5xx), el consumidor
debe considerar el estado **«sin información»**. `CauceReadClient.connectivity_or_unknown()` devuelve
`state: "engine_unreachable"`, `usable: null`. **Nunca** `unavailable` ni `usable: false`. Hay una prueba que lo verifica.

Los errores de **credencial o configuración no son «sin información»**: `401` (token inválido o revocado), `403` (permiso insuficiente) y `400`
se propagan como `CauceAPIError` y el cliente queda **detenido** (`halted`): las llamadas siguientes repiten el error sin enviar peticiones hasta que
el operador corrija la credencial y llame a `reset()`. Un `5xx` (p. ej. `503 credentials_locked`) y el motor apagado sí devuelven `engine_unreachable`
con `usable: null` (`reason` = código del error). Si el motor responde, el estado real (`unavailable` incluido) se conserva. Pruebas: `tests/test_client_errors.py`.
Otro consumidor tampoco debe usar este endpoint para concluir que un servicio externo está caído.

## 4. `GET /api/v1/status/events?since=N&limit=L`

Eventos reducidos (solo `health.change`, `health.recovered`, `inventory.added|changed|removed`, `engine.start|stop`), cada uno con
`seq`, `ts`, `kind` y, si aplica, `connection_id`, `old`, `new`. Sin mensajes, IPs ni gateways.

Diferencias deliberadas con el `/events` administrativo (que no se ha cambiado):

| | `/events` (admin, igual que v0.1.0) | `/status/events` (read) |
|---|---|---|
| Si hay más de `limit` | Devuelve los **más recientes** (hueco silencioso) | Devuelve los **primeros** tras `since`, en orden, con `has_more` y `next_since` |
| Pérdida de eventos | El consumidor debe deducirla comparando `seq` | `gap: true` calculado en el servidor |
| Reinicio del motor | No se indica | `epoch` distinto y, si `since > last_seq`, `restarted: true` |

Algoritmo del consumidor:

1. Arranque: `GET /status/connectivity` → guardar `last_seq` y `engine.epoch`.
2. Cada 5-10 s: `GET /status/events?since=<último next_since>`.
3. Si `resync_required` es `true`, o el `epoch` cambió, volver al paso 1. (`gap`: se descartaron eventos; `restarted`: motor reiniciado.)
4. Mientras `has_more` sea `true`, repetir con `since = next_since` sin esperar.

`seq` es global: los saltos entre eventos devueltos son normales (hay eventos no visibles para `read`). El hueco real lo da `gap`.
Límites: `limit` máx. 500 (200 por defecto); la memoria conserva los últimos 2000 eventos.

## 5. Lo que `read` NO puede hacer (comprobado con pruebas negativas)

`tests/test_read_tokens.py::ReadTokenDenialTest` intenta, con un token `read` real, 21 operaciones y comprueba que **no ocurrió nada**:
ni detección (`refresh`), ni sondeo (`/check`), ni rutas, selección, tareas, rotación, parada general, apagado, cambios del simulador,
creación o revocación de credenciales; no puede leer `/connections`, `/diagnostics`, `/events`, `/routes`, `/tasks` ni `/rotation`
(contienen MAC, IPs y datos de otros sistemas). Las funciones de rotación y conmutación siguen sin poder usarse para superar
límites, verificaciones o bloqueos de plataformas externas.

## 6. Límites y riesgos residuales (léelos antes de integrar)

1. **No es aislamiento entre procesos del mismo usuario de Windows.** DPAPI en ámbito de usuario permite descifrar a *cualquier*
   proceso de ese usuario, y el token de administración (`api.token`) está protegido igual. Un proceso hostil o comprometido
   que corra con tu usuario puede leerlo y obtener control total, haya o no tokens `read`.
   Los tokens `read` protegen contra **errores, consumidores demasiado permisivos y fugas del consumidor** (el consumidor nunca
   tiene la credencial de administración en su configuración, y su token solo da lectura reducida), **no** contra malware del mismo usuario.
   Aislarlo de verdad exigiría otro usuario de Windows o un servicio con ACL, que **no** está implementado.
2. El archivo del token `read` del consumidor (`--out`) queda protegido con DPAPI del usuario que lo creó. Si el consumidor corre con
   otro usuario, no podrá leerlo. En Linux/macOS se guarda con permisos 0600, **sin cifrar** (solo desarrollo).
3. Si alguien con tu usuario lee el token `read`, solo obtiene la vista reducida; revócalo con `cauce token revoke`.
4. **Sin límite de peticiones por credencial.** Un consumidor defectuoso puede saturar la API local (sin salir a la red). Pendiente.
5. La API sigue siendo HTTP en 127.0.0.1 (sin TLS), con las mismas comprobaciones de `Host` y `Origin` que v0.1.0.
6. El hash de las credenciales y los registros de acceso no son a prueba de manipulación por quien tenga tu usuario (igual que la auditoría).
7. Si `tokens.dat` queda ilegible (p. ej. otro usuario de Windows), **ninguna** credencial `read` es válida (falla cerrado).
8. Cantidad y nombres de conexiones, tipo (`wifi`, `mobile`…) y latencias sí se exponen: son lo mínimo para decidir si hay conectividad.
9. Probado solo contra el simulador y CI. Falta: Windows 11 físico, un consumidor real y la conmutación con dos proveedores (hardware).

10. **Errores de almacenamiento: fallo seguro (ver §6bis).** Una revocación solo se informa como completada si quedó escrita **y releída** en disco.
    Si no, el almacén se **bloquea**: ninguna credencial `read` se acepta (`503 credentials_locked`) hasta que una escritura verificada incluya
    la revocación. El token de administración no se ve afectado. Residual que ningún software evita: si el disco no admite **ninguna** escritura
    y el proceso muere antes de recuperarse, no queda constancia; en ese caso la operación se informó como **no completada** y, si tras reiniciar
    una credencial que se intentó revocar sigue válida, hay que revocarla de nuevo.

## 6bis. Persistencia y bloqueo de fallo seguro

| Mecanismo | Qué hace |
|---|---|
| Escritura atómica verificada | Temporal + `fsync` + reemplazo atómico + `fsync` del directorio + **relectura y comparación** con lo esperado. Cualquier fallo (escritura, reemplazo, relectura distinta) cuenta como fallo. El archivo anterior no se corrompe. |
| Bloqueo | Tras un fallo al revocar: estado `persist_failed`. Todo `read` → `503 credentials_locked` (también las credenciales sanas: no se confía en un registro del que no se sabe si es el último). No se pueden crear credenciales. |
| Archivo `tokens.dat.lock` | Se escribe de inmediato por un camino **independiente del reemplazo** (solo ids, sin secretos). **Sobrevive a reinicios**: al arrancar con él, el almacén sigue bloqueado y **reaplica las revocaciones pendientes**. Se borra solo tras una escritura verificada. |
| Sonda de arranque | Si el registro existente no puede reescribirse y verificarse: `storage_not_writable`, bloqueado. Si es ilegible: `store_unreadable`, bloqueado. |
| Respuesta al operador | `DELETE /tokens/{id}` → `503 revocation_not_persisted` («REVOCACIÓN NO COMPLETADA»), nunca 200. `cauce token list` avisa del bloqueo y marca `REVOCACIÓN PENDIENTE`. Auditoría: `token.revocation_failed`, `token.recovered` (sin secretos). |
| Recuperación | Automática (reintento verificado cada 5 s mientras alguien presente una credencial, solo si el bloqueo fue por fallo de guardado), reintentando `cauce token revoke <id>`, o explícita: `cauce token recover` (`POST /tokens/recover`, solo admin). Al recuperar, las revocaciones pendientes quedan en disco y las credenciales legítimas vuelven a funcionar. |
| Registro ilegible | `recover` lo aparta como `tokens.dat.unreadable` y empieza vacío: las credenciales anteriores quedan **invalidadas** (no se reutilizan credenciales de estado no verificable) y hay que recrearlas. |
| Compatibilidad | El formato de `tokens.dat` no cambió: las credenciales creadas con la PR #5 siguen funcionando. `GET /tokens` añade `locked`, `pending_revocations`, `lock_recorded_on_disk` y `revocation_pending`; no quita campos. |

Efecto operativo: ante un fallo de disco, los consumidores reciben `503` y deben tratarlo como «sin información» (igual que «motor no responde»), nunca como permiso.

## 7. Compatibilidad con v0.1.0

- La etiqueta `v0.1.0` no se toca. Rutas, respuestas y cliente de administración existentes no cambian; el token de sesión sigue
  siendo `admin` y accede a todo como antes (la suite existente pasa sin modificaciones).
- Cambios aditivos en `AuditLog` (`epoch`, `events_page`) y nuevos archivos `tokens.dat` y `access.jsonl` en el directorio de datos.
- No se cambian rutas globales, adaptadores, firewall ni DNS, ni se abren conexiones externas.

## 8. Diferencias con el contrato propuesto (PR #4)

- B2 permitía a `read` usar `/connections`, `/connections/{id}` y `/events`. Aquí **no**: exponen MAC, IP pública, IPs locales y
  gateways, y el contrato ya recomendaba descartarlos. Se sustituyen por las vistas reducidas.
- B4 proponía `403 insufficient_scope` (implementado igual) y `Límite por token` (**no** implementado).
- B8 (`X-Cauce-Correlation-Id`) **no** está implementado.

## 9. Probar un consumidor en simulación

```
python -m cauce sim --port 8780            # terminal 1 (datos ficticios; no es hardware)
python -m cauce token create --name prueba --out prueba.token   # terminal 2
python - <<'PY'
from cauce.client import CauceReadClient
c = CauceReadClient.from_token_file("prueba.token")
v = c.connectivity(); print(v["state"], v["usable"])
print(c.events(since=v["last_seq"]))
PY
curl -s -H "Authorization: Bearer <admin>" -X POST http://127.0.0.1:8780/api/v1/sim/connections/sim-a/state -d '{"state":"down"}'   # solo simulación
python -m cauce token revoke <id>          # el consumidor recibe 401 token_revoked
```

Prueba de fallo seguro: detén el motor y ejecuta `c.connectivity_or_unknown()` → `engine_unreachable`, `usable: None`.
