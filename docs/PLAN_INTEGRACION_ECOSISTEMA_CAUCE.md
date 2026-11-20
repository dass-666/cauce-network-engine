# Plan de integración del ecosistema Cauce (propuesta)

> **Estado: PROPUESTA. No hay nada implementado.** En v0.1.0, Cauce Network Engine no se conecta con
> ningún otro motor. Este documento describe cómo podría integrarse más adelante, de forma incremental y reversible.
>
> **Verificado:** solo lo que dice sobre Cauce Network Engine, porque se revisó su código (v0.1.0, `1551223`).
> **Pendiente de revisar:** Cauce Monitor Engine y Cauce Universal Form Connector **no se han
> inspeccionado**. Todo lo que aquí se dice sobre ellos es una suposición a partir de su descripción
> funcional y está marcado como **[POR VERIFICAR]**.

## 1. Los tres motores y sus responsabilidades

| Motor | Responsabilidad que debe conservar | Lo que **no** debe asumir |
|---|---|---|
| **Cauce Monitor Engine** | Expedientes, planificación, observación y recuperación de sus operaciones | Elegir interfaces de red o cambiar rutas. Puede consultar la salud de la red, pero no gestionarla |
| **Cauce Universal Form Connector** | Interpretar formularios, relacionar datos, escribir campos y verificar el resultado antes de un envío autorizado | Decidir por qué conexión sale su tráfico. Tampoco reintentar por otra conexión si un servicio lo limita |
| **Cauce Network Engine** | Inventario de interfaces, salud, independencia de salidas, rutas por operación y políticas (límites, 429/403) | Conocer expedientes, formularios ni datos personales. Solo ve un `owner`, un `purpose` y destinos de red |

Principio general: **cada motor es dueño de su dominio**. Network Engine nunca sabrá qué es un expediente,
y los otros dos nunca tocarán la red del sistema.

## 2. Contratos que ya existen (Verificado)

Solo los tiene Cauce Network Engine:

- API HTTP/JSON `/api/v1` en `127.0.0.1`, con token de sesión. Ver `docs/API.md`.
- Contrato estable descrito en `docs/INTEGRACION.md`: rutas por *lease*, semántica de errores y versionado.
- Cliente de referencia `cauce/client.py`. Usa solo la biblioteca estándar y se puede copiar a otro proyecto.
- Registro de eventos incremental con `GET /events?since=N`.

**[POR VERIFICAR] en los otros dos proyectos:**

- Lenguaje y versión, y si pueden usar `cauce/client.py` o necesitan un cliente HTTP propio.
- Si ya exponen alguna API o algún registro de eventos, y en qué formato.
- Si se ejecutan en el mismo equipo y con el mismo usuario de Windows (el token actual está ligado a ese usuario).
- Qué datos personales manejan y dónde los guardan.
- Si alguno contiene ya lógica de red: reintentos, *proxies* o selección de interfaz.

## 3. Datos mínimos que se intercambiarían

### Fase 1, solo lectura (de Network Engine a un consumidor)

| Dato | Campo actual | Para qué |
|---|---|---|
| Hay conectividad utilizable | `available` y `health.state` de cada conexión | Que Monitor posponga una tarea si no hay red |
| Calidad | `health.avg_latency_ms`, `health.availability_pct` | Decidir si planificar ahora o más tarde |
| Redundancia real | `independence` (`confirmed_independent` frente a otros valores) | No suponer respaldo si no existe |
| Cambios | eventos `health.*`, `route.failover`, `route.failback` | Registrar en el expediente por qué una operación se retrasó |

**No se envían** al consumidor las direcciones MAC ni la IP pública si no hacen falta. Una vista reducida de
la API, solo con lo necesario, sería una mejora futura y no existe hoy.

### Fase 2, operaciones (del consumidor a Network Engine)

Las peticiones serían `POST /routes`, con `owner` igual al motor y la operación (por ejemplo `monitor:exp-123`) y
`purpose` igual a una categoría breve. **Nunca** se mandarían datos de formularios, nombres de personas ni contenido
de expedientes: el identificador debe ser opaco, como un hash o un número interno.

## 4. Desacoplamiento y dependencias circulares

- **Dependencia en un solo sentido**: Monitor y Form Connector pueden depender de Network Engine.
  Network Engine **no depende** de ninguno y nunca los llama.
- No se comparten bases de datos ni archivos internos. Solo se usa la API versionada.
- Cada consumidor debe **funcionar sin Network Engine**. Si el motor no está en marcha, usa la red normal de
  Windows y lo registra. La integración nunca debe ser un punto único de fallo.
- Monitor y Form Connector no deberían llamarse entre sí a través de Network Engine. Si necesitan comunicarse,
  será con un contrato propio, fuera de este documento.

```
Cauce Monitor Engine ─┐
                      ├──(HTTP 127.0.0.1, /api/v1, solo lectura en fase 1)──► Cauce Network Engine
Form Connector ───────┘
          (Network Engine nunca inicia conexiones hacia ellos)
```

## 5. Separar observación y operaciones que afectan al tráfico

| Nivel | Endpoints | Riesgo | Permiso propuesto |
|---|---|---|---|
| Observar | `GET /ping`, `/connections`, `/diagnostics`, `/events` | Ninguno sobre el tráfico | `leer` |
| Usar una ruta | `POST/GET/DELETE /routes` | Solo afecta al tráfico del propio consumidor | `rutas` |
| Laboratorio | `/tasks`, `/rotation`, `/selection`, `/stop-all`, `/shutdown` | Afecta al motor entero | `laboratorio` (solo el operador) |

**Hoy (Verificado):** el token da acceso a todo. Antes de la fase 2 hacen falta **tokens por sistema con
permisos**. Con esto un consumidor no podría tomar el control de las conexiones, cambiar la selección del
operador ni detener el motor. Este es el cambio de código más importante que falta, y pertenece a una versión
futura, no a v0.1.0.

## 6. Registro de operaciones y errores

- Network Engine ya tiene una auditoría encadenada con SHA‑256 y redacción de claves secretas (Verificado).
- Propuesta: cada evento incluiría el `owner`, es decir, qué sistema lo pidió. Hoy queda en los eventos de ruta,
  pero no en un campo uniforme.
- Cada motor guarda su propio registro. Para seguir una operación entre motores bastaría un **identificador de
  correlación opaco**, enviado en `purpose` o en una cabecera futura, sin datos personales.
- Los errores de un consumidor (un formulario que falla, por ejemplo) **no** se registran en Network Engine.

## 7. Protección de datos personales

- Network Engine no debe recibir datos personales. Valida la configuración y rechaza claves con nombres de secreto,
  pero no puede saber si un `purpose` lleva un nombre propio. Esa disciplina corresponde al consumidor.
- Los consumidores no deberían copiar en sus expedientes datos de red sin necesidad (IP pública, MAC).
- Todo se queda en el equipo local, sin servicios en la nube.

## 8. Servicios independientes y locales

- Cada motor se instala, arranca y se detiene por separado. Ninguno instala ni arranca a otro.
- Comunicación solo por `127.0.0.1`. Una integración entre **equipos distintos** necesitaría otro diseño
  (TLS, autenticación mutua) y una autorización aparte. Queda fuera de este plan.
- Costo $0: biblioteca estándar y HTTP local, sin intermediarios de mensajes ni servicios de pago.

## 9. Integración incremental y reversible

| Paso | Qué | Cómo se revierte | Requisito previo |
|---|---|---|---|
| 0 | Revisar los dos proyectos (lista del apartado 2) | Nada que revertir | Acceso de lectura a sus repositorios |
| 1 | **Un** consumidor, **solo lectura**: consultar salud y disponibilidad | Quitar la llamada (opción de configuración desactivada por defecto) | Paso 0 |
| 2 | Pruebas de contrato del consumidor contra `cauce sim` | Nada que revertir | Paso 1 |
| 3 | Tokens por sistema con permisos en Network Engine | Volver al token único | Decisión de abrir la fase de operaciones |
| 4 | Un consumidor pide rutas para su propio tráfico, sin conmutación (`allow_failover: false`) | Dejar de pedir rutas | Paso 3 y prueba física con dos proveedores |
| 5 | Permitir conmutación para operaciones idempotentes | `allow_failover: false` | Paso 4 estable |

Cada paso se activa con una opción de configuración, desactivada por defecto, para volver atrás sin cambiar código.

## 10. Límites que no cambian en ningún paso

- No se diseña ni se implementa nada para eludir límites por IP, verificaciones humanas (CAPTCHA, por ejemplo),
  bloqueos ni restricciones de plataformas de terceros. Una respuesta 429, 403 o 451 **detiene** la operación
  del consumidor. Nunca provoca un cambio de conexión.
- La rotación sigue siendo solo de laboratorio y nunca estará disponible para los consumidores.
- No se cambian rutas globales, adaptadores, firewall ni DNS.

## 11. Pendiente de verificar antes de integrar

| Tema | Estado |
|---|---|
| Conmutación real entre dos proveedores | PENDIENTE (hardware) |
| Selección por IP de origen con dos salidas reales en Windows | PENDIENTE (hardware) |
| Corrección de codificación en el MSI | PENDIENTE (reinstalar en el MSI) |
| Funciones, API y datos de Cauce Monitor Engine | PENDIENTE (revisión de su repositorio) |
| Funciones, API y datos de Cauce Universal Form Connector | PENDIENTE (revisión de su repositorio) |
| Tokens por sistema con permisos | PENDIENTE (decisión y desarrollo futuro) |
| Monitor tiene su propio gestor de conexiones que elige IP de origen (hallazgo de otra auditoría, no verificado aquí) | PENDIENTE: decidir un único dueño de la selección de salida antes de la fase de operaciones. Ver `CONTRATO_SOLO_LECTURA.md`, parte C |
