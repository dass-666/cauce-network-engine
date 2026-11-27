# Laboratorio integrado en Windows 11 (Monitor + Forms + Network Engine)

Lanzadores en `scripts\laboratorio_integrado\`. Todo queda en **`%USERPROFILE%\cauce-lab-integrado`**. Se puede borrar
esa carpeta entera cuando termines.

## Qué NO toca
- `C:\cauce-msi`, `%LOCALAPPDATA%\CauceNetworkEngine` ni ninguna carpeta con un `.env` de Monitor: se niega a usarlas.
- Los puertos normales (Monitor 8000, Forms 8765, Network Engine 8780). El laboratorio usa **8100, 8865, 8880 y 8870**.
  Si alguno está ocupado no arranca nada y dice qué programa lo usa. Nunca cierra programas ajenos.
- La red de Windows (rutas, adaptadores, firewall). La rotación sigue apagada.
- Datos reales. Monitor arranca con datos de demostración y su propia base dentro de la carpeta del laboratorio.

## Pasos

| Paso | Lanzador | Qué hace | Tiempo |
|---|---|---|---|
| 1 | `1_INSTALAR.cmd` | Copia Network Engine, descarga Monitor y Forms en los commits compatibles (`versiones.json`) y prepara sus entornos de Python. Necesita internet (GitHub y PyPI) | 10–15 min la primera vez |
| 2 | `2_INICIAR_Y_PROBAR.cmd` | Comprueba puertos y versiones, arranca los tres en modo **simulado**, hace el diagnóstico, apaga y vuelve a encender Network Engine, simula un corte y una recuperación, y escribe el informe. Abre Monitor en la pantalla *Red* | 3–5 min |
| — | `3_ABRIR_MONITOR.cmd` | Abre Monitor (usuario `admin`; la contraseña está en `estado.json` de la carpeta del laboratorio) | |
| — | `DIAGNOSTICO.cmd` | Versiones, puertos y estado de los tres | |
| 3 | `5_PRUEBA_FISICA.cmd` | Network Engine en modo **real**: solo observa las interfaces y sondea **tu** servidor. Te pide desconectar y reconectar un enlace | 10–15 min |
| 4 | `4_DETENER.cmd` | Detiene solo lo que arrancó el laboratorio | |

Informe: `%USERPROFILE%\cauce-lab-integrado\informes\INFORME_LABORATORIO.md`. Cada prueba indica su evidencia:
**SIMULADA**, **LOCAL** (sin cortar la red) o **FÍSICA**.

Python: se recomienda 3.13 (`py -3.13`). Monitor no tiene ruedas para 3.15.
Forms se instala sin Chromium. Si quieres rellenar formularios de laboratorio con él: `1_INSTALAR.cmd --navegador-forms`.

## Qué demuestra cada cosa

| Prueba | Evidencia | Demuestra | No demuestra |
|---|---|---|---|
| Puertos y versiones | LOCAL | Los tres conviven sin chocar y son la combinación compatible | — |
| Diagnóstico | SIMULADA (paso 2) / FÍSICA (paso 3) | Monitor lee a Network Engine con la credencial `read` | — |
| Network Engine apagado | LOCAL | Monitor sigue funcionando, no muestra nada como sano y se reengancha solo al volver | — |
| Interrupción y recuperación (paso 2) | SIMULADA | La cadena «Network Engine detecta → Monitor lo muestra» | Que un cable o un teléfono reales se detecten |
| Prueba física (paso 3) | FÍSICA | Pérdida y recuperación reales, conmutación de tareas del motor, escrituras exactamente una vez, lo que vive el navegador | Que el navegador conserve la sesión: no puede, ver LIMITACIONES |

## Si algo falla
- **«Puerto … ocupado»:** otro programa usa ese puerto. Ciérralo tú o cambia el puerto en `versiones.json`. El laboratorio no lo cierra.
- **La instalación falla en `pip`:** revisa la versión de Python (`py -3.13 --version`) y la conexión a internet.
- **Monitor no arranca:** consulta `datos\logs\monitor.log` dentro de la carpeta del laboratorio.
