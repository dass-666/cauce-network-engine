# Limitaciones técnicas y estado de verificación (v0.1.0)

**Estado: Candidato estable de laboratorio.** Instalación y detección probadas en un Windows 11 físico
(según informó el propietario, ver [evidencia/prueba-fisica-msi.md](evidencia/prueba-fisica-msi.md)).
La conmutación real entre dos proveedores sigue **pendiente**.

CI: Linux y Windows (Python 3.10, 3.12 y 3.13) en cada push; ver la pestaña Actions del repositorio.

Leyenda: **Verificado** = probado con ejecución real reproducible · **Informado** = ejecutado en el PC físico
del propietario y comunicado por él (sin registro reproducible en el repositorio) · **Simulado** = probado solo con el
simulador · **Sin verificar** = implementado pero nunca ejecutado en el entorno objetivo.

## Estado por componente

| Componente | Estado | Evidencia / qué falta |
|---|---|---|
| Lógica de salud, histéresis, retroceso | Verificado (unitario) | `tests/test_core.py::HealthTest` |
| Leases, aislamiento entre operaciones | Verificado (unitario, con hilos) | `RoutingTest` |
| Conmutación / retorno / estado de tarea | Simulado | Escenario de 10 pasos |
| No repetir operaciones no idempotentes | Simulado | `test_non_idempotent_lost_response_is_not_repeated` |
| 429/503/403 detienen sin cambiar de conexión | Simulado + sockets reales en loopback | escenario paso 10, `test_engine_real_failover_and_throttle` |
| Envío por IP de origen y verificación (IP local y la que ve el servidor) | Verificado con sockets reales **en loopback Linux** (127.0.0.1 / 127.0.0.2) | `RealNetworkTest`. Prueba el mecanismo, **no** dos salidas a internet |
| API local, token, Host/Origin, cliente | Verificado (HTTP real en 127.0.0.1) | `ApiTest` |
| Panel | Verificado en Chromium headless (modo simulación) | `docs/img/panel-sim-failover.png` |
| Cierre limpio (`cauce stop`) | Verificado en Linux | borra token y `api.json`, registra `engine.stop` |
| Auditoría encadenada | Verificado | `test_tamper_detected_and_redaction` |
| Detección Windows (PowerShell) | **Verificado en CI (Windows Server 2025) + Informado en PC físico** | `cauce doctor` y `cauce discover` se ejecutan sin errores. Esa ejecución encontró un defecto real: la NIC de una VM invitada se clasificaba como virtual y quedaba excluida; ya está corregido. También se vio que Windows puede repetir el mismo ifIndex en dos adaptadores, y ahora se generan ids únicos. La captura real está en `tests/fixtures/windows_github_runner_real.json`. **Informado en PC físico** (MSI, Windows 11 build 26200): 4 interfaces, 1 salida utilizable, Wi‑Fi, Ethernet, Bluetooth PAN y Tailscale bien clasificados. Sin probar aún con datos móviles (4G). |
| DPAPI para el token | Verificado en Windows (CI) | `test_secret_file_permissions` cifra y descifra con DPAPI en `windows-latest` |
| Instalador `install.ps1` | Verificado en CI (sin `py`, `py` roto, sin Python, ruta explícita; 3.10/3.12/3.13) + **Informado en PC físico** | `tests/test_windows_installer.py`. En el MSI: usuario normal, Python 3.13.14 sin `py` |
| Salida de consola con acentos/símbolos | Verificado en Windows (CI) | La CI encontró un fallo con cp1252, que ya está corregido |
| Acentos en nombres de adaptador («Conexión») | Verificado en Windows (CI, `powershell.exe` real) | El MSI mostró «Conexi¢n» (cp850 leído como cp1252). Corregido con salida Base64/UTF‑8; `PowerShellEncodingTest`. Falta verlo en el MSI tras reinstalar |
| Selección de interfaz en Windows por IP de origen | **Sin verificar con dos salidas reales** | Depende del modelo *strong host* de Windows (ver abajo) |
| Independencia real de dos ISP y conmutación física | **Pendiente (expresamente)** | El MSI solo tiene un gateway utilizable. Requiere hardware: `docs/HARDWARE.md` |

## Limitaciones de diseño conocidas

1. **Selección por IP de origen, no por tabla de rutas.** Es la forma de no tocar la configuración global
   ni afectar a otras apps. En Windows funciona si la interfaz elegida tiene su propia ruta por defecto
   (gateway). Si una interfaz no tiene gateway (p. ej. red local sin internet), el tráfico enlazado a su IP
   no saldrá a internet: el motor lo detecta (sondeo fallido / `route_mismatch`), no lo arregla.
2. **Linux (solo desarrollo)** usa el modelo *weak host*: enlazar la IP de origen no garantiza la interfaz
   sin reglas `ip rule`. El motor verifica la IP usada y falla si no coincide; no crea reglas.
3. **Solo IPv4.** IPv6 no se gestiona en v0.1.
4. **Solo HTTP/HTTPS y TCP** en tareas y sondeos. Sin UDP/ICMP (ICMP necesitaría privilegios).
5. **Las aplicaciones externas deben cooperar**: el motor les da una IP de origen; no puede forzar a una
   aplicación que no la use (eso exigiría un controlador o proxy, fuera de alcance y con más riesgo).
6. **"Confirmed independent"** solo significa gateway e IP pública distintos observados. Dos ISP pueden
   compartir infraestructura aguas arriba; no es detectable desde el equipo.
7. **Rotación** solo reasigna leases entre pasos, nunca a mitad de una petición. Mínimo 10 s y máximo
   60 cambios/hora, no configurables por debajo.
8. **Estado en memoria.** Tareas y leases no sobreviven a un reinicio del motor (la auditoría sí).
9. **Un solo usuario de Windows.** El token protege frente a otros usuarios locales del mismo equipo; el
   panel lleva el token en la URL (queda en el historial del navegador local).
10. **Destinos de diagnóstico por defecto** (`1.1.1.1:443`, `9.9.9.9:443`): solo se abre y cierra una
    conexión TCP. Si no estás autorizado o prefieres otros, cámbialos en la configuración.
11. **Wi‑Fi**: el motor no conecta ni gestiona redes Wi‑Fi ni guarda sus contraseñas; usa las conexiones
    que Windows ya tiene activas.
12. Durante la detección, Windows debe exponer el adaptador de datos móviles como *Mobile Broadband*;
    módems que se presentan como Ethernet USB se clasifican como `ethernet`/`tether`.
