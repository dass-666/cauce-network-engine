# API local v1

Base: `http://127.0.0.1:<puerto>/api/v1` (puerto por defecto 8780 desde 0.2.0; antes 8765, que ahora usa Cauce Forms. Variable `CAUCE_NE_PORT` o `api.port` en config.json). Formato JSON UTF‑8.

## Seguridad

| Control | Detalle |
|---|---|
| Solo loopback | El servidor solo se enlaza a `127.0.0.1`. La configuración rechaza cualquier otra dirección. |
| Token de sesión | Aleatorio (256 bits) en cada arranque. Cabecera `Authorization: Bearer <token>`. Obligatorio en todo salvo `/ping`. |
| Dónde está el token | `%LOCALAPPDATA%\CauceNetworkEngine\api.token`, cifrado con DPAPI (solo el mismo usuario de Windows puede leerlo). Se borra al cerrar. |
| Host | Solo `127.0.0.1:<puerto>` o `localhost:<puerto>` (protección contra *DNS rebinding*). Si no, `421`. |
| Origin | En POST/DELETE, si viene `Origin`, debe ser el propio panel (protección CSRF). Si no, `403`. |
| Tamaño | Cuerpo máximo 256 KiB. |

### Permisos (desde esta rama)

El token de sesión es `admin`. Además hay credenciales `read` por sistema que **solo** llegan a `/ping`, `/status/connectivity`
`/status/routers` (desde 0.2.0, ver [ROUTERS.md](ROUTERS.md)) y `/status/events` (`403 insufficient_scope` en lo demás; `401 token_revoked` si se revocó). Gestión de credenciales (solo `admin`):
`GET/POST /tokens`, `POST /tokens/recover`, `DELETE /tokens/{id}` (`503 revocation_not_persisted` si la revocación no pudo guardarse; las `read` quedan en `503 credentials_locked` hasta recuperar). Detalle, límites y riesgos en [LECTURA_PROTEGIDA.md](LECTURA_PROTEGIDA.md).

## Errores

Todas las respuestas de error tienen la forma:

```json
{"error": {"code": "connection_unavailable", "message": "texto legible", "status": 409}}
```

| status | code (ejemplos) | Significado |
|---|---|---|
| 400 | `invalid_request`, `invalid_json`, `invalid_url` | Petición mal formada |
| 401 | `unauthorized` | Falta token o es incorrecto |
| 403 | `not_owner`, `target_not_allowed`, `bad_origin` | Prohibido (p. ej. liberar la ruta de otro, destino fuera del laboratorio) |
| 404 | `unknown_connection`, `unknown_lease`, `unknown_task`, `not_found` | No existe |
| 409 | `connection_unavailable`, `no_route`, `policy_error` | Conflicto con el estado actual |
| 421 | `bad_host` | Cabecera Host no permitida |
| 500 | `internal_error` | Error interno (detalle en el registro de errores) |

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/ping` | Vida y versión. Sin token. |
| GET | `/diagnostics` | Estado del motor, modo (real/simulado), errores de detección, contadores. |
| GET | `/connections[?refresh=1]` | Inventario con salud, independencia y disponibilidad. |
| GET | `/connections/{id}` | Una conexión. |
| POST | `/connections/{id}/check` | Sondeo de salud inmediato. |
| POST | `/selection` | `{"connection_id": "win-12" \| null}` Ruta preferida del operador (null = automática). |
| GET | `/routes` | Rutas (leases) activas. |
| POST | `/routes` | Solicitar ruta autorizada (ver abajo). `201`. |
| GET | `/routes/{lease_id}` | Estado actual de una ruta (puede haber conmutado). |
| DELETE | `/routes/{lease_id}?owner=...` | Liberar. Solo el propietario. |
| GET | `/tasks` | Tareas de laboratorio. |
| POST | `/tasks` | Iniciar tarea de laboratorio con una conexión. `202`. |
| GET | `/tasks/{id}` | Estado, pasos, resultados y verificación de ruta. |
| POST | `/tasks/{id}/stop` | Detener una tarea. |
| POST | `/stop-all` | Detener todas las tareas y todas las políticas de rotación. |
| GET | `/rotation` | Políticas de rotación. |
| POST | `/rotation` | Crear política (queda **pausada**). |
| POST | `/rotation/{name}/activate\|pause\|stop` | Cambiar estado. `stop` es definitivo. |
| GET | `/events?since=N&kind=prefijo&limit=200` | Registro de auditoría incremental. |
| POST | `/shutdown` | Cierre limpio del proceso. |
| POST | `/sim/connections/{id}/state` | **Solo en modo simulación**: `{"state": "up\|down\|disconnected"}`. |

### Conexión (objeto)

```json
{
  "id": "win-12", "name": "Ethernet", "kind": "ethernet|wifi|mobile|tether|vpn|virtual|loopback|simulated|unknown",
  "link": "up|down|disabled|unknown", "ipv4": ["192.168.1.50"], "primary_ip": "192.168.1.50",
  "gateway": "192.168.1.1", "route_metric": 25, "is_virtual": false, "public_ip": null,
  "independence": "confirmed_independent|likely_independent|shared_egress|unknown",
  "egress_group": "gw:192.168.1.1", "usable": true, "available": true, "operator_selected": false,
  "active_leases": 0,
  "health": {"state": "healthy|degraded|down|unknown", "availability_pct": 100.0, "avg_latency_ms": 12.3,
             "last_latency_ms": 11.9, "consecutive_fail": 0, "last_error": null, "backoff_level": 0}
}
```

### Solicitar ruta — `POST /routes`

```json
{"owner": "mi-app", "purpose": "sincronizar", "connection_id": null, "allow_failover": true, "allow_failback": true}
```

- `connection_id` explícito: si no está sana, **falla** (`409 connection_unavailable`); nunca se sustituye en silencio.
- Sin `connection_id`: se usa la elegida por el operador o, si no, la mejor disponible.
- Respuesta: el lease con `bind.source_ip`. La aplicación debe **enlazar su socket a esa IP** antes de conectar.
  Antes de cada conexión nueva, consulta `GET /routes/{id}`: si hubo conmutación, `source_ip` cambia.

### Iniciar tarea — `POST /tasks`

```json
{
  "name": "prueba-lab", "owner": "operador", "connection_id": "win-12",
  "steps": [
    {"method": "GET", "url": "http://127.0.0.1:8770/ok"},
    {"method": "POST", "url": "http://127.0.0.1:8770/write", "body": "{}", "idempotency_key": "op-001"}
  ],
  "allow_failover": true, "allow_failback": true, "rotation_policy": null,
  "max_attempts": 3, "step_timeout_s": 10, "max_duration_s": 300, "step_delay_s": 0
}
```

- Las URL deben pertenecer a `lab.allowed_hosts` (si no, `403 target_not_allowed`).
- `idempotent` se deduce del método (GET/HEAD/OPTIONS/PUT/DELETE) y puede forzarse por paso.
- Estados finales: `completed`, `stopped`, `throttled` (429 o 503+Retry‑After), `failed`, `needs_review`
  (respuesta perdida en operación no idempotente: no se repite).
- Cada resultado incluye `local_ip`, `observed_ip` (la que vio el servidor) y `route_verified`.

### Rotación — `POST /rotation`

```json
{"name": "lab-rr", "pool": ["win-12", "win-25"], "interval_s": 60, "max_changes_per_hour": 20}
```

Mínimos de seguridad no configurables: `interval_s ≥ 10`, `1 ≤ max_changes_per_hour ≤ 60`, pool ≥ 2.
Una tarea vinculada (`rotation_policy`) que recibe una limitación **pausa** la política automáticamente.

### Eventos

Cada evento: `{"seq", "ts", "kind", "message", "data", "prev", "hash"}`. Tipos principales:
`inventory.*`, `health.change`, `health.recovered`, `route.selected`, `route.lease`, `route.release`,
`route.failover`, `route.failback`, `route.rotation`, `route.verified`, `route.mismatch`,
`task.*`, `service.limit`, `rotation.*`, `engine.*`, `error`.
`hash` encadena cada evento con el anterior; `cauce verify-audit` detecta ediciones.


## Cambios en 0.2.0

- Puerto por defecto **8780** (ver [PUERTOS.md](PUERTOS.md)).
- Con el motor real (`cauce run`) la rotación está **apagada**: `POST /rotation` y `.../activate` responden
  `403 rotation_disabled` salvo `"lab": {"rotation_enabled": true}` en la configuración de un laboratorio propio.
  El simulador la conserva para el escenario.
- `GET /status/routers` (`read`): observación de routers, solo simulada en esta versión.
- `GET /diagnostics` añade `rotation_enabled` y `routers`.
