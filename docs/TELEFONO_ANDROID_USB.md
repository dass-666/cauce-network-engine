# Teléfono Android como segundo enlace por USB (Windows 11)

No cambia rutas, métricas, adaptadores ni firewall. Todo lo hace Windows solo al conectar y desconectar.

## Antes
- **Otro operador:** usa un teléfono con datos móviles de un operador distinto al de internet de casa u oficina.
- **Wi‑Fi del teléfono apagado:** si no, el teléfono comparte ese mismo Wi‑Fi y solo hay una salida (F2 falla).
- Pon el teléfono a cargar y quita el ahorro de batería, que puede cortar el anclaje.
- Consumo estimado: menos de 50 MB en 15 minutos (sin medir).

## Conectar
1. Usa un cable USB **de datos** (algunos cables solo cargan).
2. En el teléfono: Ajustes → Conexiones / Redes → Zona Wi‑Fi y módem → **Módem USB**. El nombre cambia según la marca.
3. Windows añade un adaptador «Remote NDIS…». Comprueba en PowerShell (solo lectura):
   ```
   Get-NetAdapter | Select Name,InterfaceDescription,Status
   Get-NetIPInterface -AddressFamily IPv4 | Sort InterfaceMetric | Select InterfaceAlias,InterfaceMetric,ConnectionState
   ```
   No cambies métricas. Windows suele dejar el Wi‑Fi o el cable como principal.
4. Si usas Tailscale u otra VPN, no hace falta apagarla. El guion ignora los adaptadores VPN y virtuales al contar enlaces.

## Durante la prueba
Cuando el guion lo pida, **desconecta el enlace principal** y vuelve a conectarlo cuando te lo indique:
- si es Wi‑Fi, desde la barra de tareas;
- si es cable, quitándolo.

## Al terminar
Desactiva «Módem USB» en el teléfono y desconecta el cable.
