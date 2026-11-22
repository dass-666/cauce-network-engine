# Cauce Network Engine

Motor local para **descubrir, supervisar, seleccionar, conmutar y rotar** varias conexiones de internet
en Windows 11, con una **API versionada solo en 127.0.0.1** para que otras aplicaciones pidan una ruta.

- Python 3.10+, **solo biblioteca estándar** (cero dependencias externas).
- No modifica la tabla de rutas de Windows ni afecta a otras aplicaciones: cada operación envía su
  tráfico enlazando el socket a la IP de la conexión elegida y **verifica** después por dónde salió.
- No guarda contraseñas ni credenciales. El único secreto (token de sesión de la API) se cifra con DPAPI.
- La rotación es solo para laboratorio propio, con límites duros; **nunca** se usa para sortear límites,
  bloqueos ni restricciones de terceros: una respuesta 429/403 detiene la operación.

> **0.2.0.dev0 (rama `claude/consolidacion-integracion`)**: API en el puerto **8780** (8765 queda para Cauce Forms,
> ver [docs/PUERTOS.md](docs/PUERTOS.md)); rotación **apagada** con el motor real; observación de routers MikroTik/OpenWrt
> **solo simulada** ([docs/ROUTERS.md](docs/ROUTERS.md)); paquete de validación física con dos enlaces
> ([docs/VALIDACION_FISICA_WINDOWS.md](docs/VALIDACION_FISICA_WINDOWS.md)), todavía **sin ejecutar** en hardware real.
>
> **v0.1.0 — Candidato estable de laboratorio.** Instalación y detección verificadas en Windows 11
> físico (PC MSI, según informó su propietario: [evidencia](docs/evidencia/prueba-fisica-msi.md)) y en la CI
> de GitHub (Linux y Windows, Python 3.10/3.12/3.13). La **conmutación real entre dos proveedores está
> expresamente pendiente**: solo se ha probado con el simulador y con sockets en loopback.
> Ver [docs/LIMITACIONES.md](docs/LIMITACIONES.md). Plan de integración futura (propuesta): [docs/PLAN_INTEGRACION_ECOSISTEMA_CAUCE.md](docs/PLAN_INTEGRACION_ECOSISTEMA_CAUCE.md). Inventario de la API y contrato de solo lectura (propuesta): [docs/CONTRATO_SOLO_LECTURA.md](docs/CONTRATO_SOLO_LECTURA.md). Seguimiento de sesiones de navegador para Monitor y Forms (simulado): [docs/SESIONES_NAVEGADOR.md](docs/SESIONES_NAVEGADOR.md).

## Inicio rápido

```bash
python -m cauce scenario            # escenario de 10 pasos con el simulador, genera informe
python -m cauce sim --open          # simulador + panel en el navegador
python -m cauce doctor              # diagnóstico del equipo
python -m cauce fieldcheck --simulate --lab-url http://lab.sim:8770   # guion de validación física (simulado)
python -m cauce routers             # routers observados (solo transporte simulado)
python -m cauce discover            # interfaces reales detectadas
python -m cauce run --open          # motor real + panel
cd tests && python -m unittest -v   # pruebas
```

En Windows: [docs/WINDOWS.md](docs/WINDOWS.md) (instalación aislada con `scripts\windows\install.cmd`).

## Arquitectura

```
cauce/
  discovery/   detección: windows.py (PowerShell, solo lectura), linux.py (desarrollo)
  independence.py  ¿comparten salida? (gateway / IP pública)
  health.py    sondeos TCP a destinos autorizados, histéresis, retroceso exponencial
  routing.py   leases por operación (resolución de rutas, aislamiento, conmutación, retorno)
  net.py       transporte real: socket enlazado a IP de origen + verificación
  policy.py    rotación controlada con límites duros
  executor.py  tareas de laboratorio: idempotencia, limitación, verificación de ruta
  engine.py    orquestador
  audit.py     auditoría append-only con hashes encadenados y redacción de secretos
  api.py       API HTTP /api/v1 en 127.0.0.1 (token, Host, Origin)
  web/         panel local
  client.py    cliente Python para integración externa
  simulator.py simulador de conexiones
  scenario.py  escenario reproducible de 10 pasos
  labserver.py servidor de laboratorio propio (eco de IP, 429 de prueba, página de sesión)
  routers/     observación de routers (contrato + MikroTik/OpenWrt simulados, solo lectura)
  fieldcheck.py validación física guiada con dos enlaces reales
  doctor.py    diagnóstico de dependencias y permisos
  secrets_store.py  DPAPI (Windows)
```

Flujo de una operación: `POST /routes` o `POST /tasks` → lease propio → cada paso toma una "foto" de su
conexión → envío enlazado a esa IP → verificación (`local_ip` y la IP que ve el servidor) → si la
conexión cae de verdad (umbral de fallos), el motor conmuta **solo ese lease** y el siguiente paso
sale por la alternativa.

## Entregables

| # | Entregable | Dónde |
|---|---|---|
| 1 | Código funcional | `cauce/` |
| 2 | Repositorio y rama | ver mensaje de entrega |
| 3 | Instalación Windows | `scripts/windows/`, [docs/WINDOWS.md](docs/WINDOWS.md) |
| 4 | Panel local | `cauce/web/index.html`, captura en `docs/img/` |
| 5 | API documentada | [docs/API.md](docs/API.md) |
| 6 | Simulador reproducible | `python -m cauce scenario`, informe en [docs/evidencia/scenario-report.md](docs/evidencia/scenario-report.md) |
| 7 | Pruebas ejecutadas | `tests/`, salida en [docs/evidencia/](docs/evidencia/), CI en `.github/workflows/ci.yml` (Linux + Windows) |
| 8 | Limitaciones técnicas | [docs/LIMITACIONES.md](docs/LIMITACIONES.md) |
| 9 | Hardware para pruebas reales | [docs/HARDWARE.md](docs/HARDWARE.md) |
| 10 | Contrato de integración | [docs/INTEGRACION.md](docs/INTEGRACION.md), `cauce/client.py` |
