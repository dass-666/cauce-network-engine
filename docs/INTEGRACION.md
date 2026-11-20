# Contrato de integración futura (v1)

Objetivo: que cualquier aplicación externa pueda consultar el estado y pedir una ruta **sin conocer
nada interno del motor** y sin que el motor conozca a la aplicación.

## Lo que la aplicación externa puede asumir (estable en v1)

1. **Transporte**: HTTP/1.1 + JSON en `http://127.0.0.1:<puerto>/api/v1`. Nada fuera de loopback.
2. **Descubrimiento local**: `%LOCALAPPDATA%\CauceNetworkEngine\api.json` (`port`, `pid`, `mode`, `version`)
   y `api.token` (cifrado DPAPI). Si no existen, el motor no está en marcha.
3. **Autenticación**: `Authorization: Bearer <token>`. El token cambia en cada arranque: reléelo si recibes `401`.
4. **Contrato de ruta**: `POST /routes` devuelve un `id` opaco y `bind.source_ip`. La aplicación:
   - enlaza sus sockets a `source_ip` (puerto 0);
   - antes de cada conexión nueva relee `GET /routes/{id}` (la ruta puede haber conmutado);
   - libera con `DELETE /routes/{id}?owner=<su owner>` al terminar.
5. **Aislamiento**: el `owner` es el único que puede liberar su ruta. Ninguna otra operación puede cambiar
   su conexión, salvo el propio motor por conmutación (si `allow_failover`), retorno (si `allow_failback`)
   o una política de rotación a la que se haya vinculado explícitamente.
6. **Semántica de errores**: códigos `error.code` estables (ver `API.md`). Mensajes (`message`) pueden cambiar.
7. **Limitaciones de terceros**: si un servicio responde 429/403, la aplicación debe detenerse o esperar.
   El motor no ofrece ningún mecanismo para reintentar por otra conexión y nunca lo hará.

## Lo que NO forma parte del contrato

- Nombres de módulos, clases o archivos internos (`cauce.*`), salvo `cauce/client.py`.
- El formato exacto de `audit.jsonl` en disco (usar `GET /events`).
- Los identificadores de conexión entre reinicios en distintos equipos (`win-<ifIndex>` puede cambiar
  si Windows renumera adaptadores). Para persistir preferencias, usar el `name` del adaptador.

## Versionado

- Cambios compatibles (campos nuevos, endpoints nuevos) se añaden en `v1`.
- Cambios incompatibles irán en `/api/v2`, manteniendo `v1` al menos una versión menor.
- `GET /ping` devuelve `api` y `version` para comprobar compatibilidad.

## Cliente de referencia

`cauce/client.py` (solo biblioteca estándar, se puede copiar a otro proyecto):

```python
from cauce.client import CauceClient
c = CauceClient.from_local_install()
estado = c.connections()
ruta = c.request_route(owner="mi-app", purpose="sync")           # o connection_id="win-12"
sock = c.connect_via(ruta, ("servidor.de.mi.lab", 443))           # socket ya enlazado y verificado
c.release_route(ruta["id"], owner="mi-app")
```

Ejemplo completo: `examples/integracion_externa.py`. Hoy **no** hay ningún sistema externo conectado.
