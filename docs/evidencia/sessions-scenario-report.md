# Escenario de sesiones de navegador (SIMULACIÓN)

> Simulación: conexiones ficticias, reloj simulado y API real en 127.0.0.1. **No** se abrió ningún navegador,
> no se visitó ningún sitio y no se envió ningún formulario ni reserva. No prueba hardware ni Windows físico.

- Resultado: **PASS**
- Versión 0.1.0 · Python 3.13.18 · Linux-6.18.44-fc-v726-x86_64-with-glibc2.39
- 30 min simulados, latido cada 5 s por sesión

## Fallos inyectados

- min 2.0: sim-a cae (sin internet)
- min 5.0: sim-a se recupera
- min 10.0: B deja de latir (navegador B colgado)
- min 12.5: B vuelve a latir
- min 16.7: A informa error page_unreachable
- min 17.0: A vuelve a ok
- min 21.7: sim-b se desconecta (sin enlace)
- min 25.0: sim-b reconecta

## Comprobaciones

### 1. Registro de dos sesiones autorizadas, cada una con su credencial y su conexión
- PASS: ambas en 'pending' antes del primer latido — `{"nav-a": "pending", "nav-b": "pending"}`
- PASS: una tercera sesión se rechaza (máximo 2) — `"too_many_sessions"`

### 2. Aislamiento de credenciales: B intenta actuar sobre A
- PASS: latido, lectura y cierre cruzados devuelven 403 not_session_owner — `["not_session_owner", "not_session_owner", "not_session_owner"]`
- PASS: A no recibió ningún latido ajeno

### 3. 30 min simulados, latido cada 5 s, con fallos inyectados
- PASS: ningún latido rechazado durante el bucle — `[]`
- PASS: muestras por estado (nav-a) — `{"active": 319, "network_degraded": 2, "network_down": 35, "browser_error": 4}`
- PASS: muestras por estado (nav-b) — `{"active": 295, "stale": 24, "network_down": 41}`

### 4. Caída y recuperación de sim-a: solo afecta a nav-a
- PASS: nav-a pasó a network_down y volvió a active — `[["pending", "active"], ["active", "network_degraded"], ["network_degraded", "network_down"], ["network_down", "active"], ["active", "browser_error"], ["browser_error", "active"]]`
- PASS: nav-b siguió active durante toda la caída de sim-a — `["active"]`

### 5. Latidos interrumpidos en B: stale solo en nav-b, recuperación al volver
- PASS: nav-b: active -> stale -> active — `[["pending", "active"], ["active", "stale"], ["stale", "active"], ["active", "network_down"], ["network_down", "active"]]`
- PASS: nav-a nunca estuvo stale — `{"active": 319, "network_degraded": 2, "network_down": 35, "browser_error": 4}`
- PASS: nav-a siguió active mientras B no latía — `["active"]`

### 6. Error informado por A: browser_error solo en nav-a
- PASS: nav-a: active -> browser_error -> active — `[["pending", "active"], ["active", "network_degraded"], ["network_degraded", "network_down"], ["network_down", "active"], ["active", "browser_error"], ["browser_error", "active"]]`
- PASS: nav-b nunca vio browser_error — `{"active": 295, "stale": 24, "network_down": 41}`

### 7. Desconexión del enlace de sim-b: solo afecta a nav-b
- PASS: nav-b pasó a network_down y volvió — `[["pending", "active"], ["active", "stale"], ["stale", "active"], ["active", "network_down"], ["network_down", "active"]]`
- PASS: nav-b estuvo network_down mientras sim-b no tenía enlace — `["network_down"]`
- PASS: nav-a siguió active durante la desconexión de sim-b — `["active"]`

### 8. Datos mínimos: el latido rechaza URL o datos de página; la vista read no expone IP ni credenciales
- PASS: latido con 'url' rechazado — `"unsupported_field"`
- PASS: sin IP ni ids de credencial en /status/sessions ni /status/events — `[]`

### 9. Estado final
- PASS: ambas sesiones active al final — `{"nav-a": "active", "nav-b": "active"}`
- PASS: aislamiento declarado — `{"one_credential_per_session": true, "shared_connection": false}`
- PASS: cierre limpio de ambas
