# Observación de routers externos (contrato, 0.2.0)

> **Estado: contrato y adaptadores implementados SOLO con transportes simulados.** No se ha probado con ningún router
> real (no hay hardware). Las respuestas simuladas siguen el formato documentado por cada fabricante, pero no son
> capturas reales. Nada aquí cambia la configuración de un router.

## Por qué

La auditoría del 2026-11-26 concluyó que la conmutación del **navegador** (observador de Monitor y Forms) debe hacerla
un router con multi-WAN, no el PC. Network Engine pasa a **observar** lo que hace el router y a anotarlo, con la misma
API de solo lectura que ya consume Monitor.

## Contrato (`cauce/routers/contract.py`)

| Elemento | Qué es |
|---|---|
| `RestReadTransport.get(path)` | Transporte REST de solo lectura (estilo MikroTik). No tiene `put/patch/post/delete` |
| `UbusReadTransport.call(obj, method)` | Transporte ubus de solo lectura (estilo OpenWrt), con lista blanca: `mwan3 status`, `network.interface dump` |
| `RouterObserver.observe() -> RouterSnapshot` | Lo que implementa cada fabricante |
| `RouterSnapshot` | `state` (`usable`/`degraded`/`unavailable`/`unknown`), `reason`, `active_wan`, `wans[]` |
| `WanStatus` | `label`, `kind`, `link_up`, `internet` (`online`/`offline`/`unknown`), `active`, `latency_ms`, `loss_pct` |

Reglas comprobadas en `tests/test_routers.py`:
1. Solo lectura por construcción: cualquier escritura o llamada fuera de la lista blanca lanza `ReadOnlyViolation`.
2. Sin IP, MAC, gateways, nombres de interfaz ni credenciales en la salida.
3. Router mudo o respuesta rara = `unknown`, nunca `offline`.
4. El adaptador no decide ni conmuta: solo observa.
5. La configuración rechaza `host`, usuario, contraseña o un transporte que no sea `simulated`.

## Adaptadores incluidos

| Fabricante | Lee | Convención en el router |
|---|---|---|
| MikroTik RouterOS v7 (`mikrotik.py`) | `GET /rest/interface`, `/rest/ip/route`, `/rest/tool/netwatch` | Cada WAN tiene el mismo **comentario** en su ruta por defecto y en su netwatch (`WAN1`, `WAN2`) |
| OpenWrt + mwan3 (`openwrt.py`) | `ubus call mwan3 status`, `ubus call network.interface dump` | Las interfaces WAN de mwan3 con su seguimiento (`track_ip`) |

Escenarios simulados: `normal`, `wan1_down`, `both_down`, `garbage`.

## Uso

```json
"routers": [{"name": "router-lab", "vendor": "mikrotik", "transport": "simulated", "scenario": "wan1_down"}]
```

- `python -m cauce routers` muestra lo observado.
- `GET /api/v1/status/routers` (credencial `read`) → `cauce.routers/1`, siempre con `"simulated": true` en esta versión.
- Cada cambio de estado o de WAN activa queda en la auditoría como `router.state` y aparece en `/status/events`.

## Añadir otro fabricante

1. Un archivo `cauce/routers/<fabricante>.py` con una clase `observe()` que solo use su transporte de lectura.
2. Respuestas simuladas en `cauce/routers/fixtures/<fabricante>.json` con los cuatro escenarios.
3. Registrarlo en `ADAPTERS` (`cauce/routers/__init__.py`). Las pruebas parametrizadas lo cubren solas.

## Fase siguiente (necesita hardware y tu aprobación)

1. Capturar respuestas REALES del router (solo lectura) y guardarlas en `tests/fixtures/routers/`.
2. Transporte real: HTTPS a la red local, usuario del router con permisos solo de lectura (MikroTik: grupo `read` con
   `api,rest-api,read`; OpenWrt: ACL de rpcd limitada a las dos llamadas), contraseña guardada con DPAPI
   (`secrets_store.py`), nunca en `config.json`.
3. Comprobar que el estado observado coincide con lo que se ve al desconectar la WAN 1 (paquete de validación física).
