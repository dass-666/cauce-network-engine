# Contrato de integración futura (v1)

Objetivo: que cualquier aplicación externa pueda consultar el estado y pedir una ruta **sin conocer
nada interno del motor** y sin que el motor conozca a la aplicación.

## Lo que la aplicación externa puede asumir (estable en v1)

1. **Transporte**: HTTP/1.1 + JSON en `http://127.0.0.1:<puerto>/api/v1`. Nada fuera de loopback.
2. **Descubrimiento local**: `%LOCALAPPDATA%\CauceNetworkEngine\api.json` (`port`, `pid`, `mode`, `version`)
   y `api.token` (cifrado DPAPI). Si no existen, el motor no está en marcha.
3. **Autenticación**: `Authorization: Bearer <token>`. El token cambia en cada arranque: reléelo si recibes `401`.
4. **Contrato de ruta**: `POST /routes` devuelve un `id` opaco y `bind.source_ip`. La aplicación:
   - enlaza sus sockets a `source_ip` (puerto 0);
   - antes de cada conexión nueva relee `GET /routes/{id}` (la ruta puede haber conmutado);
   - libera con `DELETE /routes/{id}?owner=<su owner>` al terminar.
5. **Aislamiento**: el `owner` es el único que puede liberar su ruta. Ninguna otra operación puede cambiar
   su conexión, salvo el propio motor por conmutación (si `allow_failover`), retorno (si `allow_failback`)
   o una política de rotación a la que se haya vinculado explícitamente.
6. **Semántica de errores**: códigos `error.code` estables (ver `API.md`). Mensajes (`message`) pueden cambiar.
7. **Limitaciones de terceros**: si un servicio responde 429/403, la aplicación debe detenerse o esperar.
   El motor no ofrece ningún mecanismo para reintentar por otra conexión y nunca lo hará.

## Lo que NO forma parte del contrato

- Nombres de módulos, clases o archivos internos (`cauce.*`), salvo `cauce/client.py`.
- El formato exacto de `audit.jsonl` en disco (usar `GET /events`).
- Los identificadores de conexión entre reinicios en distintos equipos (`win-<ifIndex>` puede cambiar
  si Windows renumera adaptadores). Para persistir preferencias, usar el `name` del adaptador.

## Versionado

- Cambios compatibles (campos nuevos, endpoints nuevos) se añaden en `v1`.
- Cambios incompatibles irán en `/api/v2`, manteniendo `v1` al menos una versión menor.
- `GET /ping` devuelve `api` y `version` para comprobar compatibilidad.

## Cliente de referencia

`cauce/client.py` (solo biblioteca estándar, se puede copiar a otro proyecto):

```python
from cauce.client import CauceClient
c = CauceClient.from_local_install()
estado = c.connections()
ruta = c.request_route(owner="mi-app", purpose="sync")           # o connection_id="win-12"
sock = c.connect_via(ruta, ("servidor.de.mi.lab", 443))           # socket ya enlazado y verificado
c.release_route(ruta["id"], owner="mi-app")
```

Ejemplo completo: `examples/integracion_externa.py`. Hoy **no** hay ningún sistema externo conectado.

---

# Integración futura con sistemas autorizados (diseño; **no implementado**)

Esta sección describe cómo otros sistemas del ecosistema Cauce podrán usar el motor cuando se autorice.
En v0.1.0 **no existe ninguna conexión** con otros motores o proyectos, y el motor no conoce a ninguno.

## Principio

El motor es el **único** que decide qué conexión usa cada operación y el único que aplica las políticas.
Un sistema consumidor solo **consulta** y **pide**; nunca cambia rutas de Windows, adaptadores ni la
conexión de otra operación. Si el motor no está en marcha, el consumidor funciona igual sin él (usa la
red normal de Windows, sin enlazar a ninguna IP).

## 1. Consultar interfaces, estado y salud (solo lectura)

| Necesidad | Llamada | Cliente |
|---|---|---|
| ¿Está el motor en marcha y es compatible? | `GET /api/v1/ping` (sin token) | `ping()` |
| Inventario: tipo, IP, gateway, enlace, independencia | `GET /connections` | `connections()` |
| Salud de cada conexión: estado, latencia, disponibilidad, último error | campo `health` de cada conexión | `connection(id)` |
| Estado general: modo real/simulado, errores de detección | `GET /diagnostics` | `diagnostics()` |
| Cambios desde la última consulta (conmutaciones, caídas, límites) | `GET /events?since=N` | `events(since=N)` |

- No hay notificaciones *push* en v1: el consumidor **sondea** `GET /events?since=<último seq>`
  (recomendado cada 5 s o más; es local y barato).
- `available` y `usable` ya combinan enlace, salud y exclusiones: el consumidor no debe recalcularlos.
- `independence` solo vale `confirmed_independent` si se observaron gateway e IP pública distintos; un
  consumidor que necesite redundancia real debe exigir ese valor, no contar interfaces.

## 2. Solicitar operaciones dentro de las políticas del motor

| Operación | Llamada | Qué controla el motor |
|---|---|---|
| Pedir una ruta para tráfico propio | `POST /routes` `{owner, purpose, connection_id?, allow_failover?}` | Solo conexiones sanas; si se pide una concreta y no está sana, `409` (nunca la sustituye en silencio) |
| Saber por dónde sale ahora | `GET /routes/{id}` antes de cada conexión nueva | La ruta puede haber conmutado por una caída confirmada |
| Liberar | `DELETE /routes/{id}?owner=` | Solo el propietario |
| Tarea de laboratorio | `POST /tasks` | Destinos limitados a `lab.allowed_hosts`; 429/503 detienen, 403/451 fallan, sin otra conexión |
| Rotación | `POST /rotation` y `/activate` | Solo laboratorio; nace pausada; mínimo 10 s, máximo 60 cambios/hora |
| Parada de emergencia | `POST /stop-all` | Detiene tareas y rotaciones |

Políticas que **ningún consumidor puede desactivar** por la API: no reintentar por otra conexión ante
429/403/451; no repetir operaciones no idempotentes sin `Idempotency-Key`; no cambiar rutas globales;
API solo en 127.0.0.1; límites de rotación.

## 3. Cómo debe comportarse un consumidor

1. Leer `api.json` y el token con `CauceClient.from_local_install()`; si fallan, seguir sin el motor.
2. Pedir una ruta por operación lógica, con un `owner` propio y estable (p. ej. `cauce-monitor:sync`).
3. Enlazar sus sockets a `bind.source_ip` (o usar `connect_via`, que además verifica la IP usada).
4. Ante `401`, releer el token (el motor se reinició). Ante `409 connection_unavailable`, esperar o pedir
   ruta sin `connection_id`. Ante 429/403 de un servicio externo, **detenerse**: no pedir otra ruta.
5. Liberar la ruta al terminar.

## 4. Lo que falta antes de conectar el primer sistema (recomendación)

| Tema | Hoy (v0.1.0) | Recomendado antes de integrar |
|---|---|---|
| Autorización | Un token por arranque con acceso completo | Tokens por sistema con permisos (`leer`, `rutas`, `laboratorio`) y registro del sistema que hizo cada petición |
| Contrato | Documentado en este archivo y `API.md` | Pruebas de contrato que el consumidor ejecute contra el simulador (`cauce sim`) |
| Acceso remoto | Solo 127.0.0.1 | Mantenerlo. Una conexión entre motores o equipos exige otro diseño (TLS, autenticación mutua) y una autorización aparte |
| Hardware | Una salida física probada | Prueba de dos proveedores (`docs/HARDWARE.md`) antes de que un consumidor dependa de la conmutación |

Integrar primero un consumidor **de solo lectura** (consultar estado y salud) es lo de menor riesgo:
no cambia el tráfico de nadie y valida el contrato.
