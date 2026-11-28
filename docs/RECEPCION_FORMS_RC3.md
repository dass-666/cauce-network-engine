# Recepción de la integración nativa de Forms sobre Network Engine RC3

Estado a 2026-11-28 (E9). Esto prepara **cómo se recibe y se comprueba** el trabajo de Ronaldo. No toca Forms ni lo
sustituye. RC3 sigue congelada en `f93dd58`. Estas herramientas viven en la rama `claude/e9-recepcion-forms`, que parte
de RC3 y solo añade `scripts/recepcion_forms/`, esta guía y sus pruebas.

## 1. Contrato

- **Legible por máquina:** `scripts/recepcion_forms/contrato_forms_rc3.json`. Contiene:
  - los commits de RC3;
  - las huellas SHA-256 de `cauce/client.py` y `cauce/session_consumer.py`;
  - el ámbito de la credencial, la etiqueta, los latidos, las llamadas permitidas y prohibidas, y las reglas.
- **Explicado:** `docs/CONTRATO_SESIONES_FORMS.md` (llamadas, respuestas y reglas que nunca se rompen).
- **Tareas de Ronaldo:** `PENDIENTES_RONALDO_FORMS.md` (E8b).

## 2. Comprobaciones, en este orden

| # | Qué | Orden | Qué demuestra | Qué NO demuestra |
|---|---|---|---|---|
| 1 | Estática | `python scripts/recepcion_forms/comprobar_forms.py RUTA_FORMS --salida DIR` | rama limpia que parte de `aa1bb02`; cliente y consumidor idénticos a RC3; commit anotado; sin credencial admin/review; no resuelve marcas; espera de 5 s o más; etiqueta estable; sin «sin internet»; sin FONACOT; hosts solo locales | Comportamiento. Es heurística: un REVISAR puede ser un falso positivo. |
| 2 | Dinámica desde el motor | `python scripts/recepcion_forms/verificar_sesiones_forms.py --etiqueta forms-ventanilla-1 --forms RUTA_FORMS --salida DIR --cortes 2 --reinicio-en-curso -- ORDEN_DE_PRUEBA_DE_FORMS` | Ver la lista de abajo. | Que el envío real funcione; nada físico. |
| 3 | Contrato del motor | `python -m pytest tests/test_contrato_forms.py tests/test_contrato_forms_no_repeticion.py` en el clon de RC3 | El consumidor de referencia cumple el contrato con el motor real. | El código de Forms. |
| 4 | Laboratorio integrado | rama propia con `versiones.json` apuntando al commit de Forms, luego `scripts\candidatas\VALIDAR_RC3.cmd rapido --no-interactivo` | Los tres motores juntos, con Chromium real en el portal de laboratorio y red simulada. | Prueba física. El 09b sigue usando el consumidor de referencia (ver §4). |
| 5 | Físico | `VALIDAR_RC3.cmd completo`, con Diego en la MSI | Los enlaces reales. | La continuidad del navegador durante un cambio físico de proveedor, que sigue sin probarse. |

Lo que comprueba el paso 2:
- que Forms registró una sola sesión con su etiqueta estable y con la credencial `session` de su puesto;
- que no llamó a rutas ajenas (resolver marcas, tokens, `/sim`);
- que no hubo ningún 429 ni ningún 401/403;
- que, tras un reinicio brusco del motor con la operación «en curso», la sesión queda «revisar a mano» y Forms **no la repite**;
- que la marca no desaparece mientras la sesión sigue viva.

Detalles del paso 2:
- La orden de prueba la decide Ronaldo: un modo de prueba de Forms contra el portal de laboratorio, sin enviar.
- La orden recibe el puerto y el **archivo** de la credencial por variables de entorno. Los nombres por omisión son `CAUCE_NE_PUERTO` y `CAUCE_NE_TOKEN_FILE`, y se cambian con `--env-puerto` y `--env-token`.
- Se probó con un Forms ficticio (`forms_ficticio.py`):
  - el modo «correcto» da 12/12;
  - el modo «repite» lo detecta y sale con código 1;
  - lo cubre `tests/test_e9_recepcion_forms.py`.
- Con el Forms actual (`aa1bb02`) la comprobación estática da **NO_APTO**, como se espera: todavía no lleva el cliente de RC3.

## 3. Credenciales

- Forms usa **una credencial `session` por instalación**, con el nombre de la etiqueta del puesto. Se crea con `cauce token create --name forms-ventanilla-1 --scope session --out <archivo>` (DPAPI en Windows).
- **Nunca** admin, `review` ni `read`. El paso 2 lo comprueba en el registro de accesos del motor (`who` y `scope` de cada llamada).
- La credencial `review` es solo de Monitor y solo del laboratorio (`monitor-lab-revisiones`, E8b).

## 4. Sustitución futura del consumidor del paso 09b

- **Hoy:** `scripts/laboratorio_integrado/sesion_laboratorio.py` hace de «dueño» de la sesión con el consumidor de referencia y Chromium sobre el portal de laboratorio.
- **Propuesta.** No se hace en RC3, que está congelada. Iría en la candidata que incorpore Forms y solo con la autorización de Diego:
  1. Forms expone un modo de prueba sin envío. Recibe puerto y archivo de credencial, y escribe en un JSON las fases vistas y si repitió algo.
  2. El 09b pasa a tener dos variantes, con el mismo guion de 8 fases:
     - «referencia», la actual;
     - «forms», que lanza ese modo con el Python de Forms.
  3. El informe dice cuál se usó. Mientras no exista el modo de Forms, el 09b sigue con la referencia.
  4. Criterio para sustituirla: 09b «forms» PASA en CI Windows y en la MSI, y el paso 2 de §2 da 12/12 con el mismo commit.
- **Advertencia.** Ni el 09b ni el paso 2 prueban la continuidad del navegador ante un cambio **físico** de proveedor. Un sondeo TCP tampoco la demuestra.

## 5. Manifiesto de versiones

Plantilla: `scripts/recepcion_forms/MANIFIESTO_INTEGRACION_PLANTILLA.json`. Para cada componente se anotan el commit completo y el árbol git, y además:
- el resultado de las comprobaciones 1 a 4 con la ruta de su evidencia;
- quién autorizó.

Si un commit cambia, el manifiesto se rehace entero.

## 6. Procedimiento de actualización reversible (solo laboratorio)

1. **Rama nueva** en Network Engine desde `f93dd58`, por ejemplo `claude/integracion-forms-<commit>`. Lo único que cambia es `scripts/laboratorio_integrado/versiones.json`: el pin de Forms, con su commit completo.
2. **Comprobaciones 1 y 2** sobre la copia de Forms. Si no salen APTO y 12/12, no se sigue.
3. **Instalación aparte**, en un laboratorio propio con su propio desplazamiento de puertos (por ejemplo +200). Nunca en el de RC2 ni en el de RC3, ni en `C:\cauce-msi` ni en `%LOCALAPPDATA%\CauceNetworkEngine`.
4. **Validación «rápido»**, luego «prolongado» y luego, con Diego, «completo».
5. **Volver atrás:** `DETENER` del laboratorio nuevo y borrar su carpeta. RC2 y RC3 no se tocaron. La huella SHA-256 de RC2 lo demuestra antes y después.
6. **Fusiones, envíos reales e instalación en el equipo del piloto:** nada sin la autorización expresa de Diego.

Siguen prohibidos los envíos reales, los portales externos (FONACOT ni se usa ni se sondea) y los datos personales reales.
