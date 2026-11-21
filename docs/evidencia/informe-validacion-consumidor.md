# Informe: validación de un consumidor externo con credencial `read`

**Alcance y honestidad de la evidencia.** Todo es **simulación de laboratorio**: simulador de red + API real en `127.0.0.1` + cliente `CauceReadClient`
(`examples/consumidor_lectura.py`). **No** es prueba de hardware, **no** se ejecutó en Windows físico y **no** se probó con Cauce Monitor real.
Base: `main` @ 201708c (PR #4 y #5). v0.1.0 intacta; no se tocó la MSI, ni conexiones físicas, ni otros repositorios.

## Resultado

- Pruebas nuevas: `tests/test_consumer_validation.py` — **14 pruebas, todas OK** (salida en `pruebas-consumidor-lectura.txt`).
- Suite completa tras el cambio: **109 pruebas OK (5 omitidas, las mismas de antes)**, Python 3.13.18, Linux. Dos ejecuciones consecutivas de las nuevas: sin intermitencias.
- **CI de GitHub (Linux y Windows, Python 3.10, 3.12 y 3.13): 12/12 verde** sobre este PR. **No ejecutado:** Windows físico.
- Se reutilizó la base de PR #5 (`test_read_tokens.py`: lista blanca de campos, 401, revocación, huecos, recorrido exhaustivo de rutas). Aquí solo lo que faltaba: comportamiento de un consumidor *real* de extremo a extremo.

## Fase 1 — qué se comprobó y dónde

| Requisito | Prueba | Evidencia |
|---|---|---|
| Crear credencial `read` | `Base.setUp` (PR #5) + `test_cli_create_to_file_list_revoke` (PR #5) | existente |
| `/status/connectivity` y `/status/events` | `test_uses_only_read_credential_and_only_status_paths` | el consumidor solo toca esas 2 rutas, todo 200 |
| No usar la credencial admin | misma prueba | en `access.jsonl` lo único con identidad admin es el `POST /tokens` del `setUp`; el resto es `monitor` |
| Denegar operaciones admin | `test_each_forbidden_action_is_denied_and_has_no_effect` + recorrido exhaustivo (PR #5) | 14 acciones con el cliente admin completo y token read → 403 y efectos nulos |
| Revocación | `test_revoked_credential_halts_and_stops_hitting_the_engine` | 401 `token_revoked`, luego **cero** peticiones adicionales |
| Recuperación de errores | `test_locked_store_503_...`, `test_garbage_credential_...`, `test_forbidden_scope_halts` | 503 → «sin información» con espera creciente; 401/403 → detenerse |
| Motor apagado | `test_engine_off_is_no_info_with_backoff_then_recovers` | `usable=None`, esperas 10/20/40/60 s, vuelve a `usable` al regresar el motor (mismo puerto y datos; la credencial sobrevive) |
| Sin datos de red sensibles | `test_logs_never_contain_ip_mac_or_tokens_...`, `test_result_dict_has_no_machine_data` + `test_no_machine_data_in_any_read_response` (PR #5) | ni IP, ni MAC, ni token, ni puerto en logs del consumidor ni en sus resultados |
| Reinicios | `test_engine_restart_detected_by_epoch_even_when_seq_is_higher` | caso difícil: el motor nuevo ya superó el `last_seq` anterior; solo el `epoch` lo delata → detectado |
| Pérdida de eventos | `test_event_loss_triggers_resync_not_silence` | `gap` → resincronización una sola vez |
| `usable: null` | `test_unknown_is_null_and_does_not_become_false` | `unknown`/`None`, nunca `False` |
| Interruptor de apagado | `KillSwitchTest` | variable o archivo: cero peticiones, sin errores aunque el motor esté apagado |
| Frecuencia | `test_polling_interval_has_a_floor` | mínimo 5 s impuesto |

## Fase 3 — un consumidor `read` NO puede

Crear rutas, seleccionar conexiones, rotar salidas (crear y activar), ejecutar/detener tareas, iniciar sondeos, refrescar interfaces, detener el motor,
crear credenciales (admin o read), listar o revocar credenciales, leer `/connections` (MAC/IP). Cada intento: `403 insufficient_scope`, y después se
verifica que **no** hubo `refresh`, ni sondeos nuevos, ni apagado, ni selección, ni rutas/tareas/rotaciones, ni credenciales nuevas.
Más el recorrido de **todas** las rutas registradas (PR #5): el conjunto accesible a `read` es exactamente las dos vistas `/status/*`.

## Hallazgos

| # | Gravedad | Hallazgo | Estado |
|---|---|---|---|
| 1 | Baja (documentación/cliente) | `CauceReadClient.connectivity_or_unknown()` solo convertía errores de red en `engine_unreachable`; un `503 credentials_locked` o un `401` lanzaban `CauceAPIError`, contra lo que dice `LECTURA_PROTEGIDA.md` §3. | **Corregido en el PR #7** (fusionado antes que este PR): 5xx y motor apagado → `usable: null`; 401/403/400 se propagan y detienen el cliente hasta un `reset()` explícito. El consumidor de ejemplo y el cliente quedan compatibles (`tests/test_client_errors.py`). |
| 2 | Informativa | DPAPI no aísla procesos del mismo usuario de Windows: un proceso con tu usuario puede leer el token `read` **y** `api.token`. El token read protege contra errores/fugas del consumidor, no contra malware del mismo usuario. | Documentado (guía §10). Sin mitigación implementada. |
| 3 | Informativa | Sin límite de peticiones por credencial: un consumidor defectuoso puede saturar la API local. | Documentado; el consumidor de referencia impone mínimo 5 s. |

## Qué sigue sin verificar

Windows 11 físico con un consumidor real; Cauce Monitor real (su repositorio no se inspeccionó); dos proveedores de internet; comportamiento
bajo carga. (El CI de GitHub sí pasó: 12/12.)

## Cómo reproducir

```
cd tests && python -m unittest -v test_consumer_validation      # 14 pruebas, ≈8 s
cd tests && python -m unittest                                  # suite completa, 109 pruebas
```
