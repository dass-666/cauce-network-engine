# Puertos locales del ecosistema Cauce (0.2.0)

Todos escuchan solo en `127.0.0.1`. Cada motor tiene su **propio** ajuste; ninguno lee el de otro.

| Proceso | Puerto por defecto | Cómo cambiarlo |
|---|---|---|
| Cauce Monitor (FastAPI) | 8000 | `MONITOREO_PUERTO` en el `.env` de Monitor |
| Portal de laboratorio de Monitor | 8200 / 8201 | configuración de Monitor (solo laboratorio) |
| Cauce Forms, interfaz web | 8765 | `python -m cauce_form_connector.web.app --port N` |
| Servidor de laboratorio de Network Engine | 8770 | `cauce labserver --port N` |
| **Cauce Network Engine, API** | **8780** (antes 8765) | `CAUCE_NE_PORT=N` o `"api": {"port": N}` en `config.json` |

Por qué se movió Network Engine y no Forms: Forms ya está validado en la MSI con 8765 y su interfaz es la que abre la
persona. Network Engine no está en uso operativo, así que moverlo no rompe nada en marcha.

**Compatibilidad:** una instalación existente con `config.json` que fije `8765` sigue usando 8765. El cliente
`CauceClient.from_local_install()` lee el puerto real de `api.json`, así que no hace falta cambiar nada en él. Un consumidor
que fije el puerto a mano (p. ej. Monitor con `MONITOREO_NETWORK_PUERTO`) debe usar el mismo valor que el motor.

Comprobar que no chocan: `scripts\windows\validacion_fisica\TRES_MOTORES.ps1` (solo lectura).
