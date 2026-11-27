# Servidor de validación propio para F4, F5, F8 y F9 (E5)

## Qué está listo [probado en Linux, CI pendiente]
- `cauce labserver` es el servidor propio. Usa solo la biblioteca estándar de Python, cuesta 0 pesos y maneja datos ficticios.
- **Privado y autenticado:**
  - nunca escucha en una dirección pública; lo rechaza;
  - fuera de 127.0.0.1 exige una clave de al menos 20 caracteres;
  - todas las rutas cuelgan de `/<clave>/…`; sin la clave responde 404 y no cuenta nada; la comparación es en tiempo constante.
- **Registros mínimos:** solo en memoria y acotados (2000 peticiones, 10 000 claves de escritura, 20 sesiones). Se guarda la
  ruta sin la consulta, así que la clave nunca queda escrita. No guarda cuerpos.
- **Arranque y parada:**
  - Windows: `scripts\servidor_validacion\SERVIDOR_VALIDACION.cmd <IP>`;
  - Linux o macOS: `servidor_validacion.sh <IP>`;
  - para pararlo, Ctrl+C.
- `fieldcheck --lab-url http://<IP>:8770/<clave>` conserva la ruta con la clave.
- **Evidencia:**
  - `tests/test_servidor_validacion.py`: 9 pruebas;
  - fieldcheck simulado con URL con clave: F8 ok;
  - Chromium real contra `/<clave>/session`: 6 latidos, 0 fallidos.

## Qué NO se puede hacer sin tu autorización: el límite de fondo
Para que F6–F9 prueben la **conmutación**, el servidor tiene que ser alcanzable desde los dos enlaces, el Wi‑Fi de casa
y los datos del Samsung. Los datos móviles llegan por internet, así que el servidor tendría que ser alcanzable **desde
internet**. Eso exige alguna de estas opciones, todas prohibidas en E5 sin tu permiso:

| Opción | Coste | Qué implica | Estado |
|---|---|---|---|
| Redirigir un puerto en el router de casa | 0 | Abre un puerto público | **Descartada por ti en E3** |
| Tailscale Funnel | 0 | Publica una URL en internet | Requiere tu autorización |
| Máquina virtual gratuita en la nube | 0 con límites | Crear una cuenta | Requiere tu autorización |
| Túnel gratuito (p. ej. de Cloudflare) | 0 | Publica una URL | Requiere tu autorización |

**Tailscale sin Funnel no sirve para la conmutación.** El tráfico a una IP 100.x sale por la interfaz del túnel, no por el
Wi‑Fi ni por el USB. Así se mediría el túnel, no el enlace. Solo vale como red privada para la prueba parcial de abajo.

## Qué SÍ se puede hacer ya, sin exponer nada: prueba parcial en red local
Hace falta un **segundo equipo tuyo** en el Wi‑Fi de casa (otro PC o portátil, con Python 3.11 o superior y una copia de
Network Engine). **No sabemos si lo tienes: falta confirmarlo.**
1. En ese equipo, ejecuta `SERVIDOR_VALIDACION.cmd 192.168.x.y`. Si el firewall pregunta, permite solo «Redes privadas».
2. En la MSI, ejecuta `python -m cauce fieldcheck --lab-url http://192.168.x.y:8770/<clave>`. Copia la clave de
   `clave.txt` a mano; no la pegues en chats.
3. Resultado esperado:
   - F4 (latidos del navegador), F5 (tarea larga por el enlace principal) y F8 (escrituras exactamente una vez,
     **sin corte**): pueden aprobarse físicamente por el enlace 1;
   - F6–F9 con corte: **fallarán** porque los datos móviles no llegan a tu red local. No es un fallo del software y **no
     vale como validación de conmutación**.

## Resumen honesto
- Con lo autorizado hoy, F4, F5 y F8 pueden aprobarse físicamente solo en un enlace, y solo si existe ese segundo
  equipo.
- F9 y las versiones de F4–F8 con corte necesitan que autorices una de las opciones de la tabla.
