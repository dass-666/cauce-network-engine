# Instalación y operación en Windows 11

> Estado: `install.ps1`, `doctor` y `discover` se ejecutan en la CI de GitHub (Windows Server 2025) y, según informó
> el propietario, en un PC MSI con Windows 11 build 26200 y Python 3.13.14 (ver `evidencia/prueba-fisica-msi.md`).

## Requisitos

- Windows 10/11 con PowerShell 5.1+ (incluido) y el módulo NetAdapter (incluido).
- Python 3.10 o superior (probado con 3.10, 3.12 y 3.13), de python.org o de Microsoft Store. El lanzador `py` es **opcional**:
  el instalador prueba `py -3`, `python` y `python3`, comprueba la versión real de cada uno y usa el primero que sirva.
  También puedes indicarlo a mano: `install.cmd -Python C:\ruta\python.exe`.
- **No** necesita administrador.

## Instalación aislada

1. Descarga el repositorio (zip o `git clone`).
2. Doble clic en `scripts\windows\install.cmd` (o en PowerShell: `.\scripts\windows\install.ps1`).
   - Opcional: `-AddToUserPath` añade el lanzador al PATH del usuario.
3. Todo queda en `%LOCALAPPDATA%\CauceNetworkEngine\`:

```
CauceNetworkEngine\
  app\        código del motor
  venv\       entorno Python propio (no toca el Python del sistema)
  bin\        cauce.cmd, cauce-start.cmd, cauce-sim.cmd, cauce-stop.cmd
  logs\       cauce.log (registro de errores, rotativo 5 x 2 MB)
  audit.jsonl auditoría encadenada de todos los cambios
  api.token   token de sesión cifrado con DPAPI (solo mientras corre)
  api.json    puerto y PID (solo mientras corre)
```

No se modifican rutas, adaptadores, firewall, servicios ni la política de ejecución del sistema
(`install.cmd` usa `-ExecutionPolicy Bypass` solo para ese proceso).

## Uso

| Acción | Comando |
|---|---|
| Diagnóstico de dependencias y permisos | `cauce doctor` |
| Ver interfaces detectadas | `cauce discover` |
| Guardar captura real para pruebas | `cauce discover --raw captura.json` |
| Arrancar motor real + panel | `cauce-start.cmd` (o `cauce run --open`) |
| Arrancar simulador + panel | `cauce-sim.cmd` |
| Estado | `cauce status` |
| Abrir panel de nuevo | `cauce panel` |
| Cierre limpio | `cauce-stop.cmd` o Ctrl+C en la ventana |
| Servidor de laboratorio local | `cauce labserver` |
| Verificar integridad de auditoría | `cauce verify-audit` |

### Cierre limpio

`Ctrl+C`, cerrar la consola (`SIGBREAK`) o `cauce stop` → se detienen las tareas (marcadas `stopped`),
las políticas de rotación, el monitor y la API; se borran `api.token` y `api.json`; se registra `engine.stop`.

### El Windows Firewall

La API solo escucha en `127.0.0.1`, así que Windows no debería pedir permiso de firewall. Si apareciera el
aviso, puedes **denegarlo**: el motor funciona igual en local.

## Proteger tu conexión de trabajo

El motor nunca cambia la ruta de Windows, pero sondea la salud de cada interfaz. Para que ni siquiera
sondee ni use tu conexión de trabajo, copia `config.example.json`, añade su nombre en
`"discovery": {"exclude": ["Wi-Fi"]}` y arranca con `cauce --config mi-config.json run`.

## Actualizar una instalación existente

Vuelve a ejecutar `scripts\windows\install.cmd` desde la versión nueva del repositorio. Sustituye `app\` y los
lanzadores, reutiliza `venv\` si funciona y conserva `logs\` y `audit.jsonl`. Detén antes el motor (`cauce-stop.cmd`).

## Desinstalar

`powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1` (añade `-RemoveData` para borrar registros).
