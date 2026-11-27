# Guía de una página: validación integral en la MSI con el Samsung por USB-C

Todo ocurre en el **laboratorio aislado** `%USERPROFILE%\cauce-lab-integrado`, con puertos propios
(Monitor 8100, Forms 8865, Network Engine 8880). No toca `C:\cauce-msi`, `%LOCALAPPDATA%\CauceNetworkEngine`,
los respaldos, el router, el firewall ni las rutas de Windows. Solo usa datos ficticios.

## Antes (5 min)
1. **Teléfono:**
   - usa datos móviles de **otro operador** que el internet de casa;
   - **apaga su Wi‑Fi**;
   - ponlo a cargar y quita el ahorro de batería.
2. **Cable:** USB-C a USB-C **de datos**. Un cable que solo carga no sirve: el teléfono no ofrecerá «Módem USB».
3. **Código actualizado:** en la carpeta del clon de Network Engine, ejecuta `git pull` en la rama
   `claude/e4-sesiones-diagnostico`. Esta rama incluye todo lo de la `claude/consolidacion-integracion`.
4. **No cambies** métricas, rutas ni adaptadores de Windows. Tailscale puede seguir encendido.

## Ejecutar (≈25 min)
1. Doble clic en `scripts\laboratorio_integrado\7_VALIDACION_INTEGRAL.cmd`.
2. Elige **2. Completo**. Si la instalación es vieja o no existe, el lanzador la reinstala. La primera vez tarda de 10 a 20 min y necesita internet.
3. Responde solo cuando aparezca `>>>`. Para saltar un paso, escribe `omitir`. Las pausas son:

| Cuando el guion pida | Qué haces |
|---|---|
| «Conecta ahora el Samsung por USB…» | Conecta el cable. En el teléfono: Ajustes → Conexiones → Zona Wi‑Fi y módem → **Módem USB**. Pulsa Enter. |
| «Desconecta el cable USB…» | Quita el cable. Pulsa Enter. |
| «Vuelve a conectar el cable (puedes usar OTRO puerto)…» | Conéctalo, si puedes en otro puerto. Vuelve a activar **Módem USB**. Pulsa Enter. |
| «Prueba de corte REAL…» | Pulsa Enter. Cuando lo pida, **desconecta el Wi‑Fi** desde la barra de tareas. Vuelve a conectarlo cuando lo pida. |

Durante la prueba física solo se abren conexiones TCP a `github.com:443`, cada pocos segundos y durante pocos minutos.
Es lo que decidiste en E3. Las pruebas largas (prolongado y nocturno) usan solo la red simulada.

## Después
- Desactiva «Módem USB» y desconecta el cable. Windows retira el adaptador solo.
- Informe: `%USERPROFILE%\cauce-lab-integrado\informes\integral-<fecha>\INFORME_VALIDACION_INTEGRAL.md`.
  Mándalo o déjalo ahí para revisarlo. No contiene contraseñas, tokens ni MAC; el paso 16 lo comprueba.
- **Mientras corre** la validación (el paso 15 lo apaga al final), Monitor del laboratorio está en `http://127.0.0.1:8100`, pantalla **Red**
  (la contraseña ficticia del laboratorio está en `estado.json`, campo `monitor_admin`):
  - muestra si los datos son **SIMULADOS** o **MEDIDOS**;
  - el estado de cada interfaz;
  - las sesiones de navegador;
  - el resumen de la última validación.

## Cómo leer el resultado
- **PASA** con evidencia **FÍSICA**: medido en esta MSI con enlaces reales.
- **PASA** con evidencia **SIMULADA** o **LOCAL**: no prueba nada físico.
- **PENDIENTE**: faltó un requisito, por ejemplo el teléfono o tu respuesta. **No es aprobado.**
- Matriz F1–F12: «Aprobado físicamente» solo si se midió con dos enlaces reales.
  - F4, F5, F8 y F9 no pueden aprobarse físicamente con el sondeo a github.com; necesitan un servidor propio.
