# Inventario técnico de la API y contrato propuesto de solo lectura

> **Estado (conciliado con la PR #5): la Parte A describe `main` (`09ba19a`, v0.1.0). La Parte B se
> implementó en la PR #5 con una diferencia deliberada: el permiso `read` NO llega a `/connections`,
> `/connections/{id}` ni `/events`, solo a `/status/connectivity` y `/status/events`.**
> Fuente de verdad del comportamiento implementado: [LECTURA_PROTEGIDA.md](LECTURA_PROTEGIDA.md).
> Este documento conserva el inventario (A) y el contrato (B) como especificación; donde difieren, manda la PR #5.
> Etiquetas: **EXISTE** (en `main`/v0.1.0), **IMPLEMENTADO EN PR #5** (verificado por pruebas en esa rama, aún sin
> fusionar) y **PROPUESTO** (no existe).

---

## Parte A. Inventario de lo que existe

### A1. Arquitectura y módulos

| Módulo | Función |
|---|---|
| `discovery/windows.py` | Inventario con PowerShell de solo lectura (`Get-NetAdapter`, `Get-NetIPAddress`, `Get-NetRoute`, `Get-NetIPInterface`). La salida llega en Base64 de UTF‑8 |
| `independence.py` | Agrupa por gateway e IP pública: `confirmed_independent`, `likely_independent`, `shared_egress` o `unknown` |
| `health.py` | Sondeos TCP periódicos a destinos configurados, con histéresis y retroceso |
| `routing.py` | Rutas (*leases*) por operación, selección del operador, conmutación y retorno |
| `executor.py` / `policy.py` | Tareas de laboratorio y rotación con límites duros |
| `engine.py` | Orquesta lo anterior. Refresca el inventario cada 30 s (`discovery.refresh_s`) |
| `audit.py` | Eventos encadenados con SHA‑256 en `audit.jsonl`. En memoria guarda los últimos 2000 |
| `api.py` | HTTP en `127.0.0.1:8765`, `/api/v1` |
| `client.py` | Cliente Python de referencia (solo biblioteca estándar) |

### A2. Endpoints (EXISTEN)

Todos van bajo `/api/v1` y exigen `Authorization: Bearer <token>`, salvo `/ping`.

| Método | Ruta | ¿Modifica algo? | Notas para un consumidor de lectura |
|---|---|---|---|
| GET | `/ping` | No | Sin token. `{"ok", "service", "version", "api"}` |
| GET | `/diagnostics` | No | Modo, versión, contadores, `global_routes_modified: false` |
| GET | `/connections` | No | Inventario con salud y disponibilidad |
| GET | `/connections?refresh=1` | **Sí, indirectamente** | Lanza una detección con PowerShell y genera eventos. **No usar** desde un consumidor |
| GET | `/connections/{id}` | No | Una conexión. `404 unknown_connection` si no existe |
| POST | `/connections/{id}/check` | **Sí** | Genera tráfico de sondeo. No es lectura |
| GET | `/events?since=N&kind=prefijo&limit=L` | No | `L` vale como máximo 1000 y 200 por defecto |
| GET | `/routes`, `/routes/{id}`, `/tasks`, `/tasks/{id}`, `/rotation` | No | Son lecturas, pero revelan `owner` y `purpose` de otros sistemas. No las incluyo en el contrato de lectura |
| POST/DELETE | `/selection`, `/routes`, `/tasks`, `/tasks/{id}/stop`, `/stop-all`, `/rotation...`, `/shutdown` | **Sí** | Fuera del alcance de un consumidor de lectura |

### A3. Autenticación y tokens (EXISTE)

- **Un único token** aleatorio (`secrets.token_urlsafe(32)`) por cada arranque. Se compara en tiempo constante.
- Se guarda en `%LOCALAPPDATA%\CauceNetworkEngine\api.token`, cifrado con **DPAPI de usuario**. En otros
  sistemas se guarda con permisos 0600.
- `api.json` guarda el puerto, el PID y el modo. Ambos archivos se borran al cerrar limpiamente.
- Hay comprobación de `Host` (421 `bad_host`) contra *DNS rebinding*, y de `Origin` en métodos distintos de GET
  (403 `bad_origin`) contra CSRF.

**Brecha en `main`/v0.1.0 (cerrada en la PR #5 una vez fusionada): no existen permisos de solo lectura.** El token da acceso completo, incluidos `/shutdown`,
`/stop-all`, `/selection` y la rotación. Además, DPAPI de usuario permite descifrarlo a **cualquier proceso del
mismo usuario de Windows**. Por tanto:

- **No se debe entregar este token a otro motor.** Hacerlo le daría control de administración.
- Hasta que existan tokens con permisos (PR #5), una integración de lectura **no es segura** salvo como prueba de
  laboratorio con el simulador. Con la PR #5, el consumidor recibe un token `read` y nunca el de administración,
  pero **eso no aísla procesos del mismo usuario de Windows** (ver A9 y LECTURA_PROTEGIDA.md §6).

### A4. Estado y salud de una conexión (EXISTE)

Este es un ejemplo de `GET /connections/{id}` en modo simulación, con datos ficticios:

```json
{
  "id": "sim-a", "name": "SIM Fibra (principal)", "kind": "ethernet", "link": "up",
  "ipv4": ["10.10.1.20"], "primary_ip": "10.10.1.20", "gateway": "10.10.1.1",
  "mac": null, "public_ip": "198.51.100.10", "is_virtual": false,
  "independence": "confirmed_independent", "egress_group": "pub:198.51.100.10",
  "usable": true, "available": true, "operator_selected": false, "active_leases": 0,
  "health": {"state": "healthy", "availability_pct": 100.0, "avg_latency_ms": 12.0,
             "last_latency_ms": 12.0, "consecutive_fail": 0, "consecutive_ok": 1,
             "probes": 1, "failures": 0, "last_error": null,
             "last_change": 1795180611.13, "backoff_level": 0}
}
```

- `health.state` puede ser `healthy`, `degraded` (latencia de más de 800 ms), `down` (3 fallos seguidos) o
  `unknown` (sin sondeos todavía).
- `available` indica que el motor la asignaría ahora. Combina enlace, salud y exclusiones.
- En una máquina real, `mac` y `public_ip` **sí** se rellenan. Son datos del equipo que un consumidor de lectura
  no necesita.

### A5. Eventos (EXISTE)

`GET /events?since=N` devuelve `{"events": [...], "last_seq": M}`. Cada evento tiene esta forma:

```json
{"seq": 4, "ts": 1795180611.133, "kind": "health.change",
 "message": "SIM Fibra (principal): unknown -> healthy",
 "data": {"connection_id": "sim-a", "old": "unknown", "new": "healthy", "error": null, "latency_ms": 12.0},
 "prev": "d4ca…", "hash": "d86c…"}
```

Tipos relevantes para un consumidor de lectura: `health.change`, `health.recovered`, `inventory.added`,
`inventory.changed`, `inventory.removed`, `engine.start` y `engine.stop`.

Hay dos comportamientos del `/events` **administrativo** que un consumidor debe conocer (comprobados en `audit.py`;
`/status/events` de la PR #5 los corrige: devuelve los primeros eventos en orden y calcula `gap`, `restarted` y `next_since`):

1. **Si hay más eventos que `limit`, se devuelven los más recientes**, no los más antiguos. Si el primer
   `seq` recibido es mayor que `since + 1`, **se han perdido eventos**. En ese caso hay que volver a leer
   `/connections` completo.
2. Solo se guardan 2000 eventos en memoria. Al reiniciar el motor, `seq` continúa la numeración del archivo,
   pero la memoria empieza vacía.

### A6. Errores y desconexiones (EXISTE)

- Errores HTTP con la forma `{"error": {"code", "message", "status"}}`. Los códigos posibles son
  400 `invalid_request`/`invalid_json`, 401 `unauthorized`, 403 `bad_origin`/`not_owner`,
  404 `unknown_connection`/`not_found`, 409 `connection_unavailable`/`policy_error`, 413 `too_large`,
  421 `bad_host` y 500 `internal_error`.
- Interfaz desconectada: `link` deja de ser `up`, `available` pasa a `false` y se emite un evento `inventory.changed`.
- Gateway desaparecido: se emite `inventory.changed` en el siguiente refresco (como mucho 30 s). La conexión
  solo pasa a `down` cuando fallan 3 sondeos. Limitación conocida.
- Tiempo agotado en un sondeo: cuenta como fallo, con un tiempo de espera de 3 s.

### A7. Si el motor no está disponible (EXISTE)

- No existen `api.json` ni `api.token`: el motor no está en marcha.
- Si los archivos existen pero no hay respuesta (cierre inesperado), la conexión es rechazada.
  `cauce status` lo indica con el código 3 (corregido en PR #2).
- `CauceClient` lanza `OSError`/`URLError`. El consumidor debe tratarlo como **«sin información»**, no como
  «sin red».

### A8. Limitaciones de Windows y de las redes físicas

- Probado en Windows Server 2025 (CI). En Windows 11 físico **solo según lo reportado por el usuario**, con un único gateway.
- La conmutación con dos proveedores reales y la selección de salida por IP de origen en Windows están **PENDIENTES** (hace falta hardware).
- Solo IPv4. `independence` solo puede ser `confirmed_independent` si se verificó la IP pública.

### A9. Garantías y limitaciones de seguridad

| Garantía (EXISTE) | Limitación (EXISTE) |
|---|---|
| Solo escucha en 127.0.0.1, lo que se valida dos veces | Un solo token con acceso completo |
| No modifica rutas, adaptadores, firewall ni DNS | Cualquier proceso del mismo usuario puede leer el token |
| Comprueba `Host` y `Origin`, y limita el cuerpo a 256 KiB | El token del panel queda en el historial del navegador |
| El token no se escribe en los registros, y la auditoría redacta claves secretas | `/events` contiene IPs locales y gateways |
| 429, 403 y 451 detienen la operación, sin otra conexión | No hay límite de peticiones a la propia API |

---

## Parte B. Contrato propuesto de solo lectura (PROPUESTO)

### B1. Alcance

Un consumidor autorizado puede **consultar conectividad, disponibilidad y salud**. **No puede**:

- pedir rutas,
- lanzar sondeos ni refrescos,
- ver las rutas de otros sistemas,
- ni cambiar nada.

### B2. Endpoints utilizables por un token `read` (conciliado con la PR #5)

| Endpoint | Estado | Uso |
|---|---|---|
| `GET /api/v1/ping` | EXISTE | Comprobar que el motor está en marcha y la versión (público, sin token) |
| `GET /api/v1/status/connectivity` | **IMPLEMENTADO EN PR #5** | Vista reducida (esquema `cauce.connectivity/1`), sin MAC, IP pública, IPs locales ni gateway |
| `GET /api/v1/status/events?since=N&limit=L` | **IMPLEMENTADO EN PR #5** | Cambios de salud e inventario, reducidos, con `gap`, `restarted`, `resync_required` y `next_since` |

**Cambio respecto al borrador original de esta sección:** el borrador permitía a `read` usar `/connections`,
`/connections/{id}` y `/events`. Se **retiró**: esos endpoints devuelven `mac`, `public_ip`, `ipv4`, `primary_ip` y
`gateway` (y `/events` contiene IPs locales y gateways), y el propio contrato ya recomendaba descartarlos.
Un token `read` recibe `403 insufficient_scope` en ellos, y en `/diagnostics`, `/routes`, `/tasks` y `/rotation`.
Los endpoints administrativos no cambian para el token de sesión (`admin`).

### B3. Vista reducida (IMPLEMENTADA EN PR #5; el borrador inicial era distinto)

La forma real, el significado de `state`/`usable` (incluido `unknown` con `usable: null`) y los campos excluidos están en
[LECTURA_PROTEGIDA.md §3](LECTURA_PROTEGIDA.md). Resumen con datos ficticios:

```json
{
  "schema": "cauce.connectivity/1", "api": "v1",
  "engine": {"version": "0.1.0", "mode": "real", "epoch": "9f3a1c02"},
  "generated_at": "2026-11-20T13:00:00Z", "last_seq": 412,
  "state": "usable", "usable": true, "reason": null,
  "counts": {"connections": 1, "available": 1, "measured": 1},
  "connections": [
    {"id": "win-14", "kind": "wifi", "link": "up", "available": true, "measured": true,
     "health": "healthy", "availability_pct": 99.2, "avg_latency_ms": 23.5}
  ]
}
```

Cambios frente al borrador original: se añaden `schema`, `epoch`, `last_seq`, `usable` (tri-estado), `reason`, `counts`
y `measured`; se elimina `independent_exits` y `independence` (revelan agrupación de salidas y no son necesarios para
decidir si hay conectividad). El consumidor debe seguir ignorando los campos que no conozca (B7).

### B4. Autenticación y permisos (PROPUESTO)

Todo lo siguiente está **IMPLEMENTADO EN PR #5** (aún sin fusionar):

1. **Tokens por sistema con permiso `read`**, creados por el operador (`cauce token create --name monitor --out monitor.token`);
   solo se guarda su hash SHA-256 (`tokens.dat`, con DPAPI en Windows). El permiso `admin` no se puede crear.
2. En el servidor, el permiso `read` solo da acceso a los endpoints de B2. Cualquier otro devuelve `403 insufficient_scope`;
   el valor por defecto de una ruta nueva es `admin`.
3. Cada petición queda en `access.jsonl` con el nombre del sistema y el id de la credencial, nunca el token, el cuerpo ni los valores de la consulta.
4. Revocación inmediata sin reiniciar el motor (`401 token_revoked`) y persistente tras reinicios. **Nunca** se entrega el token de administración.
5. Si el almacenamiento falla al revocar, la API responde `503 revocation_not_persisted` (nunca 200), las credenciales `read` quedan
   bloqueadas (`503 credentials_locked`) y el bloqueo sobrevive a reinicios con las revocaciones pendientes reaplicadas, hasta una
   escritura verificada o `cauce token recover` (LECTURA_PROTEGIDA.md §6bis). Un consumidor trata `503` como «sin información».

**Siguen sin existir** (PROPUESTOS): límite de peticiones por token (B5/Parte C) y aislamiento real frente a procesos del mismo usuario de Windows.
**Mientras la PR #5 no se fusione, la integración no debe pasar del laboratorio.**

### B5. Tiempos y frecuencia recomendados

- Tiempo de espera por petición: **2 s** (la API es local). Si se agota, se trata como «sin información».
- Sondeo de `/events`: cada **5 a 10 s**. `/connections` completo: cada **60 s**, o cuando se detecte un hueco en `seq`.
- Si el motor no responde, reintentar con retroceso exponencial: 5 s, 10 s, 20 s, y como máximo 60 s. Nunca en bucle cerrado.

### B6. Política de fallo seguro

| Situación | Lo que debe hacer el consumidor |
|---|---|
| Motor ausente, tiempo agotado o 5xx | Estado «desconocido». **No** concluir que no hay red. Seguir con su comportamiento normal |
| 401 | Releer `api.json` y el token **una vez**. Si sigue fallando, quedarse en «desconocido» |
| 403 o 421 | Error de configuración. Detenerse y registrarlo. No reintentar |
| `health.state = down` en todas las conexiones | Puede posponer **sus propias** tareas. No puede cambiar la red |
| Un servicio externo responde 429, 403 o CAPTCHA | No tiene relación con este contrato. **Nunca** se usa el motor para sortearlo |

### B7. Versionado

- `GET /ping` devuelve `api: "v1"`. El consumidor debe rechazar una versión mayor desconocida.
- Los campos nuevos se añaden sin romper nada, así que el consumidor debe **ignorar los campos que no conozca**.
- Un cambio incompatible iría en `/api/v2`, manteniendo `v1` durante al menos una versión menor.

### B8. Identificadores de correlación (PROPUESTO, no incluido en la PR #5)

- Cabecera opcional `X-Cauce-Correlation-Id`: un UUID v4 o un hash generado por el consumidor, **sin** nombres,
  expedientes, correos ni datos de formularios.
- El motor la copiaría en sus registros de acceso. Hoy **no** la lee ni la registra.

### B9. Pruebas para validar un consumidor

Todas se hacen contra `cauce sim`, sin red real. Para un consumidor, usar `/status/connectivity` y `/status/events`
(no `/connections` ni `/events`, que un token `read` no puede leer). La parte del motor ya la cubre `tests/test_read_tokens.py`:

1. Con el motor parado, el consumidor informa «desconocido» y sigue funcionando.
2. Con un token inválido, recibe 401, relee una vez y se detiene sin bucle.
3. Al pasar `sim-a` a `down` (`POST /sim/connections/sim-a/state`, solo en simulación), recibe el `health.change` a `down` (tras 3 sondeos fallidos; con el retroceso puede tardar hasta unos 60 s).
4. Ante un hueco en `seq` (más de `limit` eventos), vuelve a leer `/connections`.
5. El consumidor nunca llama a métodos distintos de GET (se comprueba con un *proxy* de registro o con la auditoría del motor).
6. No guarda `mac`, `public_ip` ni IPs locales.
7. Con el motor detenido a mitad de una petición, no se bloquea más de 2 s.
8. Con un campo desconocido añadido en la respuesta, no falla.

---

## Parte C. Riesgos de integración

| Riesgo | Gravedad | Mitigación |
|---|---|---|
| Entregar el token actual a otro motor le da control total | **Alta** | No hacerlo. Usar tokens `read` (PR #5, pendiente de fusión) |
| **Cauce Monitor tiene su propio gestor de conexiones que elige IP de origen.** Lo encontró la auditoría de otro hilo; **no lo he verificado yo** | **Alta** | Network Engine no ve ese tráfico: sus leases y la conmutación no lo coordinan. Hay que decidir un único dueño de la selección de salida antes de cualquier fase de operaciones. En la fase de lectura, Monitor solo consulta |
| El consumidor confunde «motor caído» con «sin red» | Media | Política B6 |
| Eventos perdidos al pasar el límite | Media | Detectar huecos en `seq` (A5) |
| Fuga de datos de red (MAC, IP pública) a los expedientes | Media | Vista reducida B3, o descartar esos campos al recibirlos |
| Sondeo excesivo desde varios consumidores | Baja | Frecuencias de B5. Límite por token (PROPUESTO) |
| Usar la salud como pretexto para cambiar de salida ante un bloqueo | **Prohibido** | Fuera de contrato. 429/403/CAPTCHA detienen la operación |

## Parte D. Requisitos para los otros dos motores

**Por verificar en sus repositorios. No los he inspeccionado.**

1. Declarar si ya eligen interfaz o IP de origen, o si reintentan por otra red. Si lo hacen, documentar que esa
   función **no** se coordina con Network Engine. Para Monitor, otro hilo lo ha señalado.
2. Ejecutarse en el mismo equipo y con el mismo usuario de Windows, o documentar lo contrario (afecta a DPAPI).
3. Tratar a Network Engine como **opcional**: funcionar sin él.
4. Usar solo GET y los endpoints de B2.
5. No registrar datos de red innecesarios y no enviar datos personales en ninguna cabecera.
6. Superar las pruebas de B9 contra `cauce sim`.
7. No implementar ningún mecanismo para eludir límites por IP, CAPTCHA ni controles antiabuso.

## Parte E. Lo que no se pudo verificar

- El código de Cauce Monitor Engine y de Cauce Universal Form Connector. Lo dicho sobre el gestor de conexiones
  de Monitor viene de otra auditoría.
- El comportamiento del contrato en Windows 11 físico con dos salidas (hardware PENDIENTE).
- Que la corrección de codificación funcione en el MSI (PENDIENTE de reinstalar allí).
- Si los consumidores corren con el mismo usuario de Windows que el motor.
