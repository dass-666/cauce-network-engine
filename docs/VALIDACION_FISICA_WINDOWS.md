# Validación física con dos enlaces reales (Windows 11)

> **Qué es:** un guion (`python -m cauce fieldcheck`) que te pide desconectar y reconectar un enlace y deja un informe
> con evidencia. **Hasta que lo ejecutes en tu equipo con dos enlaces reales, la conmutación sigue sin validar.**
> En la CI solo corre con `--simulate`, y ese informe dice **SIMULADA**.

## Qué no hace (a propósito)

- No instala nada ni toca la instalación existente (`%LOCALAPPDATA%\CauceNetworkEngine`): se ejecuta desde la carpeta
  del repositorio y guarda todo (incluida su auditoría) en `reports\validacion-fisica-<fecha>\`.
- No cambia rutas, métricas, adaptadores ni el firewall. La desconexión la haces tú a mano.
- No sondea servicios de terceros: los sondeos, las tareas y la página de sesión van a **tu** servidor de laboratorio.
- No abre Monitor ni Forms: si quieres probar los tres a la vez, ábrelos antes con sus propios lanzadores.
- La rotación sigue apagada. Un 429/403 detiene la tarea como siempre.

## Lo que necesitas (sin compras)

| Elemento | Opción sin costo | Nota |
|---|---|---|
| Equipo de pruebas | La MSI (u otro Windows 11) | Usuario normal, sin administrador |
| Enlace 1 | Wi‑Fi o Ethernet de tu casa/oficina | |
| Enlace 2 | Teléfono con datos de **otro operador** por cable USB (anclaje USB) | Que no sea la misma red Wi‑Fi |
| Servidor de laboratorio **propio** | Otro equipo tuyo alcanzable desde internet en el puerto 8770 | Ver «Dónde poner el servidor» |
| Python 3.10+ | Ya está en la MSI | Network Engine no tiene dependencias externas |

### Dónde poner el servidor de laboratorio

Tiene que verse desde **los dos** enlaces, y por eso debe estar en internet, no en la red local del enlace 1.

1. **Otro equipo tuyo en otra ubicación** (p. ej. en tu casa si la MSI está en la oficina), con el puerto 8770 abierto
   en su router solo hacia ese equipo. Costo 0. Es la opción recomendada.
2. **Una máquina virtual en la capa gratuita** de un proveedor de nube. Costo 0, pero requiere crear una cuenta.
3. **Solo como prueba parcial:** otro equipo en la misma red local. Sirve para F1–F5 y F10–F12, pero el enlace 2 (datos
   móviles) **no** llegará a él, así que F6–F9 fallarán. No vale como validación de conmutación.

En ese equipo: copia el repositorio y ejecuta `scripts\windows\validacion_fisica\SERVIDOR_LABORATORIO.cmd`. Cierra la
ventana al terminar: no dejes el puerto abierto.

## Pasos

1. En la MSI, desactiva el ahorro de energía del USB del teléfono y conecta el anclaje USB. Comprueba en Windows que
   ves las dos redes conectadas.
2. (Opcional) abre Monitor y Forms con sus lanzadores si quieres el punto F11.
3. Doble clic en `scripts\windows\validacion_fisica\VALIDAR_DOS_ENLACES.cmd` y escribe la URL de tu servidor
   (p. ej. `http://tu-servidor:8770`).
4. Se abre una página en el navegador que late cada segundo. **No la cierres.** Pulsa Enter.
5. Cuando lo pida, **desconecta el enlace principal** (el que diga la pantalla): quita el cable, desactiva el Wi‑Fi o
   desconecta el teléfono, según cuál sea. Pulsa Enter y espera.
6. Cuando lo pida, **reconéctalo** y pulsa Enter.
7. Al terminar verás «SIN FALLOS» o «HAY FALLOS». Envía la carpeta `reports\validacion-fisica-<fecha>` (las IP van
   enmascaradas; aun así, revísala antes de compartirla).

Duración: unos 6 a 10 minutos.

## Qué significa cada punto del informe

| Punto | Pasa si… | Qué demuestra |
|---|---|---|
| F1 | Es Windows y se leyeron las rutas por defecto | Punto de partida |
| F2 | ≥2 enlaces utilizables con salidas distintas | Detección de dos enlaces |
| F3 | Los dos responden a tu servidor, con latencia | Estado de cada enlace |
| F4 | Tu servidor recibe latidos del navegador | La página de sesión funciona |
| F5 | La tarea larga empieza por el enlace principal | |
| F6 | Tras desconectar: caída detectada y tarea en el otro enlace (con tiempos) | **Pérdida real y conmutación** |
| F7 | Tras reconectar: enlace sano y retorno al principal | **Recuperación automática** |
| F8 | Cada escritura consta **exactamente una vez** en tu servidor | Sin repetir operaciones no idempotentes |
| F9 | El navegador sigue latiendo al final; informa huecos, recargas y cambio de IP vista por el servidor | **Lo que vive una sesión de navegador** (usa la ruta de Windows, no la del motor) |
| F10 | Cadena de auditoría íntegra | Consistencia de la auditoría |
| F11 | Monitor (8000), Forms (8765) y Network Engine (8780) responden | Los tres motores a la vez (informativo) |
| F12 | Rutas por defecto iguales antes y después | El motor no toca la red de Windows |

Cómo leer F9: un hueco de algunos segundos sin recarga de página es lo esperable (el navegador reintenta solo). Si el
servidor ve **dos IP** distintas, el tráfico del navegador cambió de proveedor; una sesión web que dependa de la IP se
perdería en ese momento. Esto es justo lo que Cauce no puede evitar desde el PC (ver la auditoría, sección 6).

## Si algo falla

- **F2 con 1 enlace:** Windows no ve el teléfono como red con salida. Revisa que el anclaje USB esté activo.
- **F2 con 2 enlaces pero «1 salida distinta»:** los dos llegan por la misma red (p. ej. el teléfono comparte el Wi‑Fi).
- **F3 falla en el enlace 2:** el servidor no es alcanzable desde datos móviles (¿está en la red local?).
- **F6 sin conmutación:** guarda la carpeta y la auditoría; es exactamente lo que queremos descubrir (modelo *strong host*
  de Windows, `docs/LIMITACIONES.md` punto 1).
