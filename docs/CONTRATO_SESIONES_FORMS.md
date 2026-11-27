# Contrato de sesiones para Cauce Forms (diagnóstico, E5)

**Estado:** propuesta probada contra Network Engine, **no integrada en Forms**. Forms pertenece a su autor y no se ha
modificado. El PR #8 sigue abierto. Este documento, `cauce/session_consumer.py` y `tests/test_contrato_forms.py`
fijan qué debe cumplir cualquier implementación.

## Qué es
Forms informa a Network Engine del **estado** de cada sesión de navegador que la persona abrió y autorizó. Network
Engine lo combina con lo que mide de la red, y Monitor lo muestra. **Nadie decide por la persona.**

## Qué NO es
- Network Engine no abre, controla ni encamina el navegador. El navegador sigue la ruta de Windows.
- No se envían URL, cookies, campos, contenido de páginas ni datos personales.
- No se repite ni se reenvía ninguna acción.
- No se rota la IP, no se cambia de conexión por visita y no se esquiva ninguna restricción de terceros.

## Credenciales
Una credencial `session` por sesión, creada por el administrador (`cauce token create forms-a --scope session`) y
guardada protegida con DPAPI. Forms no recibe ni necesita la credencial admin.

## Llamadas (API local `http://127.0.0.1:<puerto>/api/v1`)
| Momento en Forms | Llamada | Cuerpo permitido |
|---|---|---|
| La persona abre la sesión | `POST /sessions` | `label`, `connection_id` (opcional), `authorized_by_user: true`, `heartbeat_timeout_s` |
| Cada 5–10 s mientras está abierta | `POST /sessions/{id}/heartbeat` | `status` ok/error, `error_code` (lista cerrada), `operation` |
| Justo **antes** de que la persona envíe | latido con `operation: "in_progress"` | — |
| Justo **después** (salga bien o mal) | latido con `operation: "idle"` | — |
| La persona cierra | `DELETE /sessions/{id}` | — |

Los latidos deben ir separados al menos 0,5 s. Por debajo, el motor responde 429.

## Respuestas y qué debe hacer Forms
| Respuesta | Significado | Forms debe |
|---|---|---|
| 200 con `phase` | estado calculado | mostrar la fase; si `requires_human`, mostrar «revisar a mano» con `human_reason` |
| sin respuesta, o error de conexión | **sin información** | seguir funcionando; no decir «sin internet»; no bloquear a la persona |
| 404 `unknown_session` | el motor ya no la conoce (caducada, cerrada o sin persistencia) | registrarla de nuevo |
| 409 `credential_has_session` | esa credencial ya tiene otra sesión viva | no crear otra; usar el id que ya tenía |
| 401 | credencial inválida o revocada | dejar de latir y avisar al administrador |
| 429 | latidos demasiado seguidos | esperar unos segundos; no insistir |

## Reglas que nunca se rompen
1. **Una operación a medias no se repite.** Si el motor marcó `requires_human`, la persona decide si el envío se
   completó, revisando el portal o el acuse. Ningún código reintenta por ella.
2. **`requires_human` solo lo quita una persona** con credencial admin, mediante `POST /sessions/{id}/resolve`. No lo
   quitan ni un latido, ni la recuperación de la red, ni un reinicio del motor o de Forms, ni un nuevo registro.
3. **Si el motor se reinicia**, la sesión vuelve como «incierta» (`restored: true`). Si había una operación en curso,
   vuelve con `requires_human` (`engine_restart_during_operation`).
4. **Si Forms también perdió el id**, un nuevo `POST /sessions` con la misma credencial sustituye la sesión restaurada y
   **hereda** la marca.
5. **Network Engine solo observa.** Si no responde, Forms funciona igual. La acción de la persona nunca espera al motor.

## Qué demuestran las pruebas de contrato
`tests/test_contrato_forms.py` usa API real, red simulada y reinicio real del motor sobre la misma carpeta y el mismo
puerto. Comprueba:
- ciclo normal y lista blanca de campos;
- motor apagado durante el envío: la acción no se bloquea y el estado es «sin información»;
- reinicio del motor a mitad del envío: queda `requires_human` y no se repite nada;
- reinicio de Forms y del motor: la marca se hereda;
- sin persistencia, un 404 lleva a registrar de nuevo;
- 401 detiene los latidos;
- 429 no se insiste;
- la marca no se olvida si el motor cae.

## Qué falta, y es decisión del autor de Forms
- Llamar a esto desde Forms, en su repositorio y con su revisión.
- Elegir dónde se muestra «revisar a mano» en su interfaz.
- Decidir si Forms recuerda el id de sesión entre reinicios. Es opcional: la regla 4 cubre el caso.
