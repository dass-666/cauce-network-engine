# Evidencia: seguimiento de sesiones de navegador (rama feat/sesiones-navegador)

Fecha: 2026-11-22. Entorno: contenedor Linux en la nube, Python 3.13.18.

| Tipo | Qué se ejecutó | Resultado |
|---|---|---|
| Pruebas automatizadas | `cd tests && python -m unittest` (suite completa) | Ran 147 tests in 53.808s, OK (skipped=5) |
| Pruebas nuevas | `test_sessions.py` (22) + 2 en `test_scenario.py` | todas OK (lista abajo) |
| Simulación | `python -m cauce scenario-sessions`: 30 min simulados, 2 sesiones, 8 fallos inyectados | PASS ([informe](sessions-scenario-report.md)) |
| Prueba de la prueba | El escenario se ejecuta con el aislamiento roto a propósito (todas las sesiones leen la red de sim-a) | FAIL, como debe (`test_scenario_detects_broken_isolation`) |
| Hardware / Windows físico | — | **NO ejecutado** |
| Navegador real, Monitor o Forms reales | — | **NO ejecutado**. No se visitó ningún sitio ni se envió ninguna reserva |

La CI de GitHub (Linux + Windows, Python 3.10/3.12/3.13) se ejecutará al abrir la PR; su resultado se anota en la PR, no aquí.

## Pruebas nuevas ejecutadas

```
test_parallel_heartbeats (test_sessions.ConcurrentStabilityTest.test_parallel_heartbeats) ... ok
test_heartbeat_rate_limited (test_sessions.DataMinimizationTest.test_heartbeat_rate_limited) ... ok
test_heartbeat_rejects_page_data (test_sessions.DataMinimizationTest.test_heartbeat_rejects_page_data) ... ok
test_read_view_has_no_network_secrets_or_credential_ids (test_sessions.DataMinimizationTest.test_read_view_has_no_network_secrets_or_credential_ids) ... ok
test_disconnected_interface_marks_dependent_session_down (test_sessions.GlobalNetworkTest.test_disconnected_interface_marks_dependent_session_down) ... ok
test_session_without_connection_follows_global_state (test_sessions.GlobalNetworkTest.test_session_without_connection_follows_global_state) ... ok
test_both_active (test_sessions.IsolationTest.test_both_active) ... ok
test_browser_error_in_one_session_does_not_affect_other (test_sessions.IsolationTest.test_browser_error_in_one_session_does_not_affect_other) ... ok
test_events_visible_to_monitor_and_sanitized (test_sessions.IsolationTest.test_events_visible_to_monitor_and_sanitized) ... ok
test_link_failure_affects_only_dependent_session (test_sessions.IsolationTest.test_link_failure_affects_only_dependent_session) ... ok
test_other_credential_cannot_touch_session (test_sessions.IsolationTest.test_other_credential_cannot_touch_session) ... ok
test_stale_detection_is_per_session (test_sessions.IsolationTest.test_stale_detection_is_per_session) ... ok
test_admin_can_view_and_close_but_not_heartbeat (test_sessions.PermissionsTest.test_admin_can_view_and_close_but_not_heartbeat) ... ok
test_authorization_must_be_declared (test_sessions.PermissionsTest.test_authorization_must_be_declared) ... ok
test_one_session_per_credential_and_max_two (test_sessions.PermissionsTest.test_one_session_per_credential_and_max_two) ... ok
test_only_session_credentials_register (test_sessions.PermissionsTest.test_only_session_credentials_register) ... ok
test_revoked_credential_cannot_heartbeat (test_sessions.PermissionsTest.test_revoked_credential_cannot_heartbeat) ... ok
test_session_credential_cannot_read_status_or_admin (test_sessions.PermissionsTest.test_session_credential_cannot_read_status_or_admin) ... ok
test_unknown_connection_rejected (test_sessions.PermissionsTest.test_unknown_connection_rejected) ... ok
test_limits_validated (test_sessions.RegistryUnitTest.test_limits_validated) ... ok
test_pending_then_stale_without_heartbeat (test_sessions.RegistryUnitTest.test_pending_then_stale_without_heartbeat) ... ok
test_restart_forgets_sessions (test_sessions.RegistryUnitTest.test_restart_forgets_sessions) ... ok
test_ten_step_scenario (test_scenario.ScenarioTest.test_ten_step_scenario) ... ok
Prueba de la prueba: si el estado de red de una sesión ignorase SU conexión, el escenario debe fallar. ... ok
test_two_browser_sessions_scenario (test_scenario.SessionScenarioTest.test_two_browser_sessions_scenario) ... ok
```
