# Prueba física en Windows 11 (PC MSI) — resultados informados

> **Categoría: REPORTADO POR EL USUARIO.** Los informó el propietario del equipo el 2026-11-20 tras ejecutarlo en su PC. No se ejecutaron
> desde la nube ni se adjuntó la salida completa, así que aquí constan como *informados por el propietario*,
> no como verificados por la CI. La versión instalada en el MSI era el commit `bfa9c49`.

## Instalación

- `scripts\windows\install.cmd` (versión corregida, `bfa9c49`) terminó correctamente con **Python 3.13.14**
  y sin el lanzador `py`. Antes de esa corrección fallaba con «No se encontró Python 3.10+».

## `cauce doctor`

| Comprobación | Resultado informado |
|---|---|
| Sistema | Windows 11, build 26200 (entorno compatible) |
| Python | 3.13.14 |
| PowerShell y módulo NetAdapter | Disponibles |
| Privilegios | Usuario normal (sin administrador) |
| Carpeta de datos | Escribible |
| Puerto local 8765 (API local disponible) | Libre |
| Interfaces detectadas | 4 |
| Salidas físicas utilizables | 1 |

## `cauce discover`

| Interfaz | Estado | Clasificación |
|---|---|---|
| Intel Wi‑Fi 6 AX201 | Activa | Wi‑Fi física, salida potencialmente independiente |
| Realtek PCIe GbE | Desconectada | Ethernet física |
| Bluetooth PAN | Desconectada | Virtual |
| Tailscale | Activa | VPN virtual (excluida como salida) |

Observación: alguna descripción se mostró como «Conexi¢n» en lugar de «Conexión». Causa: PowerShell 5.1
escribe por tubería en la página de códigos OEM (cp850) y Python la leía como cp1252. Corregido después en
`cauce/discovery/windows.py` (el JSON se emite en Base64 de UTF-8). La corrección está probada en la CI de
Windows con `powershell.exe` real; **no** se ha vuelto a ejecutar en el MSI (habría que reinstalar).

## Qué NO se probó físicamente

- **Conmutación entre dos proveedores reales.** El equipo tiene un solo gateway utilizable (Wi‑Fi).
  La conmutación, el retorno y la rotación solo están probados en el simulador y con sockets en loopback.
- Selección de interfaz por IP de origen con dos salidas reales (modelo *strong host* de Windows).
- Independencia real de dos ISP (`docs/HARDWARE.md` describe la prueba pendiente).

## Reglas para esta evidencia

- No se accedió remotamente al MSI ni se modificó su instalación.
- La versión v0.1.0 publicada (`1551223`) incluye la corrección de codificación, que el MSI todavía no tiene
  instalada. Para comprobarla allí: reinstalar con `scripts\windows\install.cmd` y ejecutar `cauce discover`.
