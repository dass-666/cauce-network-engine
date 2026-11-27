# Seguimiento de sesiones de navegador autorizadas (propuesta de contrato con Monitor y Forms)

> **Estado: IMPLEMENTADO en esta rama y validado SOLO en simulación** (pruebas automáticas + `python -m cauce scenario-sessions`).
> No se ha probado con Cauce Monitor ni con Cauce Forms reales, ni con un navegador real, ni en Windows físico.
> No se modificó ningún repositorio de Monitor ni de Forms: lo que sigue es la **propuesta** de cómo se integrarían.
> Evidencia: [evidencia/sesiones-simulacion.md](evidencia/sesiones-simulacion.md).

## 1. Qué es y qué no es

Network Engine **observa** el estado de hasta dos sesiones de navegador que el usuario declara autorizadas y lo combina con
el estado de red que ya mide. Responde a: *«¿esta sesión está viva y la red de la que depende funciona?»*.

| Sí hace | No hace (a propósito) |
|---|---|
| Registrar una sesión por credencial `session` (máximo 2, configurable hasta 8) | Abrir, controlar o automatizar navegadores |
| Recibir latidos `ok` / `error` + código de una lista cerrada | Recibir URL, cookies, títulos, contenido ni datos de formularios (400 `unsupported_field`) |
| Calcular un estado por sesión y anotar cada cambio en la auditoría | Enviar formularios o reservas |
| Atribuir a cada sesión la salud de la conexión de la que depende | Encaminar el tráfico del navegador por una conexión concreta |
| Exponer el estado a Monitor en `/status/sessions` y `/status/events` | Asignar IP distintas por sesión, rotar direcciones o eludir límites, bloqueos o detección de servicios externos |

`connection_id` es **informativo**: el navegador sigue usando la ruta del sistema operativo. Sirve para que, si esa conexión cae,
la sesión que depende de ella aparezca como `network_down` y la otra no. Si no se indica, la sesión sigue la conectividad global.

## 2. Roles y credenciales

| Sistema | Credencial | Puede |
|---|---|---|
| Operador (Ronaldo) | token de sesión `admin` (no se entrega a nadie) | crear/revocar credenciales; ver y cerrar cualquier sesión |
| Cauce Forms, sesión A | `session` propia (`forms-a`) | registrar **una** sesión, enviarle latidos, leerla y cerrarla |
| Cauce Forms, sesión B | `session` propia (`forms-b`) | lo mismo, solo sobre la suya |
| Cauce Monitor | `read` (`monitor`) | `/status/connectivity`, `/status/events`, `/status/sessions` |

```
python -m cauce token create --name forms-a --scope session --out forms-a.token
python -m cauce token create --name forms-b --scope session --out forms-b.token
python -m cauce token create --name monitor --out monitor.token          # read (ya existía)
```

Los permisos no son jerárquicos: una credencial `session` recibe 403 en `/status/*` y una `read` recibe 403 en `/sessions`.
El token `admin` puede ver y cerrar sesiones, pero **no** registrar ni enviar latidos (403 `session_credential_required`),
para que nadie pueda fingir el estado de un navegador.

## 3. Contrato para Forms (dueño de cada sesión)

| Método y ruta | Cuerpo | Respuesta |
|---|---|---|
| `POST /api/v1/sessions` | `{"label": "nav-a", "authorized_by_user": true, "connection_id": "<opcional>", "heartbeat_timeout_s": 30}` | 201 + vista de la sesión (`id` = `bs_…`) |
| `POST /api/v1/sessions/{id}/heartbeat` | `{"status": "ok"}` o `{"status": "error", "error_code": "<código>"}` | 200 + vista |
| `GET /api/v1/sessions/{id}` | — | 200 + vista |
| `DELETE /api/v1/sessions/{id}` | — | 200 `{"closed": id}` |

- `authorized_by_user` debe ser `true`: es la declaración de Forms de que el usuario autorizó esa sesión. Network **no puede verificarla**.
- `error_code` ∈ `page_unreachable`, `navigation_timeout`, `browser_closed`, `session_expired`, `network_error`, `other`.
- Latido recomendado cada 5–10 s; más de uno cada 0,5 s → 429 `heartbeat_too_frequent`. `heartbeat_timeout_s` entre 5 y 300.
- Errores: 400 `unsupported_field` / `invalid_request` / `invalid_error_code` / `authorization_not_declared` / `invalid_label`;
  401 credencial inválida o revocada; 403 `not_session_owner`; 404 `unknown_session` (sesión cerrada, caducada o persistencia apagada: volver a registrar)
  o `unknown_connection`; 409 `credential_has_session`, `label_in_use`, `too_many_sessions`.
- Cliente de referencia (biblioteca estándar): `cauce.client.CauceSessionClient` (`register`, `heartbeat`, `get`, `close`).
- **Responsabilidad de Forms, no verificable por Network:** usar perfiles de navegador separados para cada sesión (cookies y
  almacenamiento aislados) y no enviar nada que no sea el estado.

## 4. Contrato para Monitor (solo lectura)

`GET /api/v1/status/sessions` (credencial `read`, sin parámetros) → `cauce.sessions/1`. Ejemplo real del simulador:

```json
{"schema": "cauce.sessions/1", "api": "v1", "engine": {"version": "0.1.0", "mode": "simulated", "epoch": "df6d3b79"},
 "generated_at": "2026-11-22T21:42:03Z", "last_seq": 12, "max_sessions": 2,
 "sessions": [
  {"id": "bs_d390a7f5d670", "label": "nav-a", "registered_by": "forms-a", "state": "active", "reason": null, "state_since_s": 0.0,
   "network": {"source": "connection", "connection_id": "sim-a", "state": "ok"},
   "heartbeat": {"last_age_s": 0.0, "timeout_s": 30.0, "reported": "ok", "error_code": null, "count": 1},
   "stability": {"state_changes_10m": 1, "state_changes_total": 1}},
  {"id": "bs_88075280756c", "label": "nav-b", "registered_by": "forms-b", "state": "browser_error", "reason": "page_unreachable",
   "network": {"source": "global", "connection_id": null, "state": "ok"}, "...": "..."}],
 "isolation": {"one_credential_per_session": true, "shared_connection": false}}
```

Estados (`state`, de mayor a menor prioridad) y qué debe mostrar Monitor:

| state | Significa | `reason` |
|---|---|---|
| `stale` | no llegan latidos en `timeout_s` (navegador o Forms colgado/cerrado) | `no_heartbeat`, `heartbeat_timeout` |
| `network_down` | la conexión de la que depende (o toda la red) está caída | `connection_down`, `no_usable_connection` |
| `browser_error` | Forms informó un error | el `error_code` |
| `pending` | registrada, sin primer latido | `awaiting_first_heartbeat` |
| `network_unknown` | latido ok, el motor aún no tiene mediciones | `insufficient_measurements` |
| `network_degraded` | latido ok, red lenta o con fallos parciales | — |
| `active` | latido ok y red sana | — |

Eventos nuevos en `/status/events` (mismas reglas de `since`, `gap` y `resync_required` que ya existen):
`session.registered` (`session_id`, `label`), `session.state` (`session_id`, `old`, `new`, `reason`), `session.closed` (`session_id`, `label`).

`stability.state_changes_10m` alto indica una sesión inestable (oscila). `isolation.shared_connection = true` es solo informativo:
las dos sesiones dependen de la misma conexión y caerán a la vez si esta cae.

Regla de fallo seguro (igual que la conectividad): si Network no responde, Monitor debe mostrar «sin información», **nunca** «sesión caída».

## 4b. Fases de diagnóstico y protección contra repeticiones (E4)

Además de `state` (técnico), cada sesión expone `phase`, pensada para que Monitor la muestre a una persona:

| phase | Cuándo | Qué hace la persona |
|---|---|---|
| `iniciada` | registrada, sin primer latido | nada |
| `activa` | latido ok (incluye red lenta) | nada |
| `perdida_conectividad` | la conexión de la sesión está caída | esperar |
| `recuperada` | volvió a `active` hace menos de 120 s tras `network_down`, `stale`, `browser_error` o `network_unknown` | comprobar que la página sigue donde estaba |
| `requiere_intervencion` | se interrumpió **mientras Forms declaraba `operation: "in_progress"`**, o Forms informó `session_expired` | revisar a mano y marcar como resuelta |
| `incierta` | sin latidos (`stale`) o sin mediciones de red | comprobar el navegador |
| `interrumpida` | Forms informó un error del navegador (p. ej. `browser_closed`) | reabrir la sesión |
| `cerrada` | `DELETE` de la sesión; se guardan las 10 últimas en `closed` de `/status/sessions` | nada |

- Latido nuevo: `{"status": "ok", "operation": "idle" | "in_progress"}`. Forms declara `in_progress` justo antes de una acción
  que no se puede repetir sin riesgo (enviar, confirmar) e `idle` al terminar.
- **Regla de no repetición:** si la sesión se interrumpe con una operación en curso, queda `requires_human: true` y
  **recuperar la red no lo quita**. Solo lo quita `POST /api/v1/sessions/{id}/resolve` con credencial `admin` (evento de
  auditoría `session.human_resolved`). Network Engine nunca relanza nada: no tiene forma de enviar un formulario.
  Forms debe consultar `requires_human` antes de reintentar y no reintentar si es `true`.
- Eventos de auditoría nuevos: `session.requires_human`, `session.recovered`, `session.human_resolved`.
- Prueba de laboratorio (paso 09b de `7_VALIDACION_INTEGRAL.cmd`): Chromium real en el portal propio (127.0.0.1), red
  SIMULADA, recorre las 8 fases, rellena un campo con un dato ficticio **sin enviar** y comprueba que ninguna petición POST
  salió de la página y que el motor no creó tareas. Es evidencia **simulada** (F4 y F9 «Aprobado en simulación»).
  Hasta que Forms integre el cliente, el script de laboratorio hace de dueño de la sesión; Forms no se ha modificado.

## 4c. Persistencia del estado tras reinicios (E5)

Archivo `sessions_state.json` en la carpeta de datos del motor (permisos 0600). Se activa por omisión y se apaga con
`sessions.persist: false`. Para empezar de cero: parar el motor y borrar `sessions_state.json` y `sessions_state.json.bak`.

- **Qué guarda (lista blanca):** id y etiqueta de la sesión, id (no el secreto) y nombre de la credencial dueña, conexión,
  plazo de latido, último estado, operación (`idle`/`in_progress`), marca de intervención humana y su motivo, número de
  latidos e historial de las 10 últimas cerradas. **No** guarda URL, cookies, formularios, páginas ni datos personales.
- **Escritura segura:** temporal + fsync + reemplazo atómico; la versión buena anterior queda en `.bak`; resumen SHA-256.
  Un cambio de operación o de marca se guarda al momento; los meros latidos, como mucho cada 30 s.
- **Al reiniciar:** cada sesión vuelve como `stale` / `engine_restarted` (fase «incierta», `restored: true`) hasta que su
  dueño vuelva a latir con la MISMA credencial y el mismo id.
  - Si tenía `operation: in_progress`, vuelve con `requires_human` (`engine_restart_during_operation`).
  - Una marca `requires_human` previa se conserva. **Solo `POST /sessions/{id}/resolve` (admin) la quita**: ni el latido, ni la
    recuperación de la red, ni otro reinicio, ni un nuevo registro.
  - Si el dueño perdió el id y vuelve a registrarse, la sesión restaurada se cierra como `replaced_after_restart` y la nueva
    **hereda** la marca de intervención.
- **Caducidad:** una sesión sin latidos durante `sessions.restore_max_age_s` (3600 s por omisión, 60–604800) se cierra como
  `expired` (o `expired_after_restart` si fue durante el apagado). Si tenía una operación en curso, se cierra marcada como
  `requires_human`. Nada se repite ni se da por bueno.
- **Archivo dañado:** se usa el `.bak` (auditoría `session.state_recovered_from_backup`); si también está dañado se
  empieza vacío (`session.state_corrupt`). El dañado se aparta como `.corrupt-<fecha>`, nunca se borra. Editar el archivo
  a mano invalida el resumen: no sirve para «autorizar» una sesión.
- **Fallo de escritura** (disco lleno, permisos): el motor sigue en memoria; se anota `session.state_write_failed` una vez
  y `session.state_saved` cuando vuelve a poder escribir.
- Pruebas: `tests/test_sesiones_persistencia.py` (reinicio, caída con SIGKILL, escritura cortada, archivo dañado o
  alterado, fallo de escritura, suspensión corta y larga, caducidad, 4 escritores simultáneos, ausencia de secretos).

## 5. Tiempos de detección (simulación, valores por defecto)

| Situación | Se detecta en | Depende de |
|---|---|---|
| Navegador/Forms deja de latir | `heartbeat_timeout_s` (30 s) | configuración de la sesión |
| Conexión sin internet | ~3 sondeos fallidos (≈15 s con `interval_s` 5) | `health.fail_threshold`, `health.interval_s` |
| Interfaz desconectada | siguiente detección de inventario (≤30 s) o siguiente sondeo | `discovery.refresh_s` |
| Recuperación de red | 2 sondeos correctos | `health.recover_threshold` |

En el motor real estos tiempos **no se han medido** en Windows físico.

## 6. Límites conocidos

- Desde E5 el estado de diagnóstico se guarda en disco (§4c). Con `sessions.persist: false` vuelve el comportamiento anterior: las sesiones se pierden al reiniciar.
- Network no ve el navegador: el estado `browser_*` es lo que Forms declara. Un Forms con errores puede declarar `ok` falsamente.
- Una credencial `session` revocada deja su sesión sin latidos → `stale`; el operador puede cerrarla con el token `admin`.
- Mismo límite de DPAPI que las credenciales `read`: no aísla procesos del mismo usuario de Windows.
- Sin pruebas con dos proveedores de internet reales (solo hay un gateway en el equipo de pruebas).

## 7. Qué falta para integrarlo (lista para Monitor y Forms)

1. Ronaldo autoriza y fusiona esta rama (no se ha fusionado).
2. Ronaldo crea las credenciales `forms-a`, `forms-b` y (si no existe) `monitor` con los comandos del punto 2.
3. Forms: llamar a `register` al abrir cada sesión autorizada, `heartbeat` cada 5–10 s y `close` al terminar; reintentar `register` ante 404.
4. Monitor: consultar `/status/sessions` cada 10–30 s (o al recibir `session.*` en `/status/events`) y mostrar la tabla de estados del punto 4.
5. Prueba conjunta en el MSI con Monitor, Forms y un navegador real, **sin enviar reservas**, y anotar la evidencia por separado de la simulación.
