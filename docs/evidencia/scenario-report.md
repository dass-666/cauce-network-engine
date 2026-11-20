# Cauce Network Engine — escenario funcional de 10 pasos

> SIMULACIÓN: valida la lógica del motor, NO el hardware, controladores ni el enrutamiento real de Windows.

- Versión: 0.1.0 · Python 3.13.18 · Linux-6.18.44-fc-v726-x86_64-with-glibc2.39
- Ejecutado: 2026-11-20T04:01:36+0000
- Resultado global: **OK**

## ✅ Paso 1. La primera conexión funciona
- ✓ A detectada con enlace activo e IP
- ✓ Salud de A = healthy

## ✅ Paso 2. La segunda está disponible como alternativa
- ✓ Salud de B = healthy
- ✓ A y B tienen salidas independientes (gateway e IP pública distintos)

## ✅ Paso 3. La tercera está desconectada
- ✓ C sin enlace y no utilizable
- ✓ C no disponible para rutas
- ✓ Pedir C explícitamente es rechazado (sin sustitución silenciosa)

## ✅ Paso 4. El operador selecciona una ruta
- ✓ Ruta preferida = A

## ✅ Paso 5. El motor confirma que la ruta seleccionada se utiliza
- ✓ Los 2 primeros pasos salieron por A
- ✓ IP local y IP observada por el servidor coinciden con A

## ✅ Paso 6. La primera pierde conectividad
- ✓ Caída de A detectada tras umbral de fallos (no por un único error)
- ✓ El intento por A falló SIN enviar la petición

## ✅ Paso 7. El motor conmuta a la segunda
- ✓ Evento de conmutación A -> B con motivo
- ✓ Pasos posteriores salieron por B, verificados
- ✓ Estado de la tarea conservado: ningún paso repetido ni saltado

## ✅ Paso 8. La primera vuelve
- ✓ Recuperación de A registrada (tras umbral de éxitos)
- ✓ La tarea volvió a A (failback) en un paso posterior
- ✓ Tarea completada

## ✅ Paso 9. El sistema registra todos los cambios
- ✓ Eventos presentes: health.change, health.recovered, inventory.added, route.failback, route.failover, route.lease, route.selected, route.verified, task.completed, task.throttled
- ✓ Cadena de hashes del archivo de auditoría íntegra

## ✅ Paso 10. Una respuesta de limitación detiene la operación sin usar otra conexión
- ✓ Tarea en estado throttled
- ✓ Solo 1 petición enviada, por A; ninguna por B
- ✓ Sin conmutación para esa tarea
- ✓ Segundo paso no ejecutado
- ✓ La política de rotación se pausó automáticamente (no rota para sortear el 429)
- ✓ Lease liberado y desvinculado de la rotación

## Registro de auditoría (resumen)

| # | tipo | mensaje |
|---|------|---------|
| 1 | `inventory.added` | Conexión detectada: SIM Fibra (principal) (ethernet, up) |
| 2 | `inventory.added` | Conexión detectada: SIM LTE (alternativa) (mobile, up) |
| 3 | `inventory.added` | Conexión detectada: SIM Wi‑Fi (desconectada) (wifi, down) |
| 4 | `health.change` | SIM Fibra (principal): unknown -> healthy |
| 5 | `health.change` | SIM LTE (alternativa): unknown -> healthy |
| 6 | `route.selected` | operador-escenario seleccionó SIM Fibra (principal) como ruta preferida |
| 7 | `route.lease` | Ruta SIM Fibra (principal) asignada a 'tarea-lab-1' |
| 8 | `task.created` | Tarea 'tarea-lab-1' creada con 6 pasos |
| 9 | `task.started` | Tarea 'tarea-lab-1' iniciada |
| 10 | `route.verified` | 'tarea-lab-1' paso 0: tráfico verificado por sim-a |
| 11 | `route.verified` | 'tarea-lab-1' paso 1: tráfico verificado por sim-a |
| 12 | `task.connect_failed` | 'tarea-lab-1' paso 2: no conectó por sim-a |
| 13 | `health.change` | SIM Fibra (principal): healthy -> degraded |
| 14 | `health.change` | SIM Fibra (principal): degraded -> down |
| 15 | `route.failover` | 'tarea-lab-1': sim-a -> sim-b (pérdida de conectividad confirmada en sim-a) |
| 16 | `route.verified` | 'tarea-lab-1' paso 2: tráfico verificado por sim-b |
| 17 | `health.change` | SIM Fibra (principal): down -> healthy |
| 18 | `health.recovered` | sim-a recuperada |
| 19 | `route.failback` | 'tarea-lab-1': sim-b -> sim-a (la conexión preferida se recuperó) |
| 20 | `route.verified` | 'tarea-lab-1' paso 3: tráfico verificado por sim-b |
| 21 | `route.verified` | 'tarea-lab-1' paso 4: tráfico verificado por sim-a |
| 22 | `route.verified` | 'tarea-lab-1' paso 5: tráfico verificado por sim-a |
| 23 | `route.release` | Ruta liberada (tarea-lab-1) |
| 24 | `task.completed` | Tarea 'tarea-lab-1' -> completed:  |
| 25 | `rotation.created` | Política 'lab-rr' creada (pausada) |
| 26 | `rotation.state` | Política 'lab-rr': paused -> active (escenario: rotación activa antes de la limitación) |
| 27 | `route.lease` | Ruta SIM Fibra (principal) asignada a 'tarea-limitada' |
| 28 | `task.created` | Tarea 'tarea-limitada' creada con 2 pasos |
| 29 | `task.started` | Tarea 'tarea-limitada' iniciada |
| 30 | `route.verified` | 'tarea-limitada' paso 0: tráfico verificado por sim-a |
| 31 | `service.limit` | 'tarea-limitada': el servicio pidió limitar (429); se detiene sin cambiar de conexión |
| 32 | `rotation.state` | Política 'lab-rr': active -> paused (limitación del servicio: no se rota para sortearla) |
| 33 | `route.release` | Ruta liberada (tarea-limitada) |
| 34 | `task.throttled` | Tarea 'tarea-limitada' -> throttled: el servicio respondió 429 (Retry-After=60); operación detenida sin usar otra conexión |
