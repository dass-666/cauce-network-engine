# Hardware necesario para comprobar conexiones realmente independientes

La simulación prueba la lógica. Para probar que el motor gestiona **salidas reales distintas** hace falta:

## Mínimo (prueba de conmutación real)

| Elemento | Para qué | Ejemplo |
|---|---|---|
| Equipo con Windows 11 | Ejecutar el motor | El PC de laboratorio (no el de trabajo diario, idealmente) |
| Salida 1: línea fija | Conexión principal | Ethernet al router del ISP A |
| Salida 2: **otro proveedor** | Alternativa independiente | Módem/router 4G/5G con SIM de otro operador, por Ethernet USB (p. ej. adaptador USB‑Ethernet) o teléfono en modo *tethering* USB |
| Servidor de laboratorio propio en internet | Comprobar qué IP pública ve el destino | Un VPS barato propio con `cauce labserver` adaptado o cualquier endpoint propio que devuelva la IP de origen |

Puntos clave:
- **Dos adaptadores conectados al mismo router NO sirven**: comparten gateway e IP pública. El motor los marca `shared_egress`.
- Cada salida necesita **su propio gateway** (redes distintas, p. ej. 192.168.1.0/24 y 192.168.8.0/24).
  Si los dos routers usan la misma subred, cambia una de ellas en el router (no en Windows).
- Una VPN no es una salida independiente: viaja por otra interfaz física.

## Recomendado (prueba completa de los 10 pasos con hardware)

- Tercera salida (Wi‑Fi a otra red o segundo módem) para el caso "desconectada".
- Un interruptor/enchufe para cortar físicamente la salida 1 (o desenchufar el cable WAN del router 1,
  que provoca "enlace activo sin internet", el caso más realista).
- Configurar `egress_check_url` / el servidor propio para confirmar IP pública distinta por salida
  (pasa de `likely_independent` a `confirmed_independent`).

## Procedimiento de verificación propuesto

1. `cauce doctor` y `cauce discover`: deben verse ≥ 2 conexiones con gateways distintos.
2. `cauce discover --raw captura.json` (o `scripts/windows/capture-fixture.ps1`) y añadir la captura
   (revisada) a `tests/fixtures/` para que las pruebas usen datos reales.
3. `cauce run --open`, iniciar una tarea larga contra el servidor propio por la salida 1.
4. Desenchufar el WAN del router 1 → debe aparecer `health.change … down`, `route.failover`, y los pasos
   siguientes con `observed_ip` = IP pública del ISP 2.
5. Reconectar → `health.recovered`, `route.failback`.
6. Endpoint propio que devuelva 429 → `throttled` y ningún paso por la salida 2.
7. Comprobar que el resto del equipo (navegador, otras apps) siguió usando su ruta normal: el motor no
   toca la tabla de rutas (`route print` antes y después debe ser idéntico salvo cambios del propio Windows).

## Precaución con el equipo de trabajo

El motor no cambia rutas globales, pero los sondeos de salud generan tráfico pequeño por cada interfaz.
Si una interfaz es la de trabajo y no quieres que se use, ponla en `discovery.exclude` (por nombre).
Desconectar físicamente una salida sí afecta a Windows: hazlo en el equipo de laboratorio.
