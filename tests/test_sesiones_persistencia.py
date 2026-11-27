"""E5: persistencia del ESTADO de diagnóstico de las sesiones (reinicio, caída, archivo dañado, fallo de escritura,
suspensión, caducidad). Regla central: nada que pedía persona o estaba en curso queda autorizado tras un reinicio."""
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import sim_engine
from test_read_tokens import SIM_SECRETS  # noqa: F401  (carga el entorno de pruebas igual que el resto)
from test_sessions import FakeClock, SessionApiBase
from cauce.audit import AuditLog
from cauce.session_store import SessionStore
from cauce.sessions import SessionRegistry

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Reg:
    """Registro con almacén real en un directorio temporal; `restart()` simula un reinicio del motor."""

    def __init__(self, d=None, net="ok", max_age=3600.0):
        self.d = d or tempfile.mkdtemp(prefix="cauce-ses-")
        self.path = os.path.join(self.d, "sessions_state.json")
        self.clock = FakeClock()
        self.net = {"v": net}
        self.max_age = max_age
        self.audit = AuditLog(os.path.join(self.d, "audit.jsonl"))
        self.r = self._new()

    def _new(self, store=None):
        return SessionRegistry(self.audit, lambda cid: self.net["v"], lambda cid: True, clock=self.clock,
                               store=store or SessionStore(self.path), restore_max_age_s=self.max_age)

    def restart(self, store=None):
        self.r = self._new(store)
        return self.r

    def kinds(self):
        return [e["kind"] for e in self.audit.events(limit=5000)]

    def reg(self, owner="tok-a", label="forms-a"):
        return self.r.register(owner, "forms", {"label": label, "authorized_by_user": True, "heartbeat_timeout_s": 30})

    def beat(self, sid, owner="tok-a", **kw):
        self.clock.advance(1)
        return self.r.heartbeat(sid, owner, {"status": "ok", **kw})


class ReinicioTest(unittest.TestCase):
    def test_reinicio_normal_restaura_como_incierta_hasta_el_latido(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="idle")
        r = h.restart()
        v = r.view(r.all()[0])
        self.assertEqual((v["id"], v["state"], v["reason"], v["phase"], v["restored"]),
                         (s.id, "stale", "engine_restarted", "incierta", True))
        self.assertFalse(v["requires_human"])
        h.beat(s.id, operation="idle")
        v = r.view(r.all()[0])
        self.assertEqual((v["state"], v["restored"]), ("active", False))
        self.assertIn("session.restored", h.kinds())

    def test_operacion_en_curso_al_reiniciar_exige_persona_y_no_se_limpia_sola(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        r = h.restart()
        v = r.view(r.all()[0])
        self.assertEqual((v["requires_human"], v["human_reason"], v["phase"]),
                         (True, "engine_restart_during_operation", "requiere_intervencion"))
        h.beat(s.id, operation="idle")                       # ni el dueño
        h.restart()                                          # ni otro reinicio
        self.assertTrue(h.r.all()[0].requires_human)
        h.r.resolve(s.id)                                    # solo una persona
        self.assertFalse(h.r.all()[0].requires_human)
        h.restart()
        self.assertFalse(h.r.all()[0].requires_human, "la revisión humana también se conserva")

    def test_marca_previa_de_intervencion_sobrevive_reinicios(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.net["v"] = "down"
        h.r.evaluate()
        self.assertEqual(h.r.all()[0].human_reason, "operation_interrupted")
        h.net["v"] = "ok"
        for _ in range(3):
            h.restart()
        self.assertEqual((h.r.all()[0].requires_human, h.r.all()[0].human_reason), (True, "operation_interrupted"))

    def test_nuevo_registro_del_dueno_hereda_la_marca(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.restart()
        n = h.reg()                                          # el dueño también se reinició y perdió el id
        self.assertNotEqual(n.id, s.id)
        self.assertEqual((n.requires_human, n.human_reason), (True, "engine_restart_during_operation"))
        self.assertIn("session.replaced", h.kinds())
        closed = h.r.summary()["closed"][0]
        self.assertEqual((closed["reason"], closed["requires_human"]), ("replaced_after_restart", True))

    def test_sesion_no_restaurada_no_se_sustituye(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id)
        with self.assertRaises(Exception):
            h.reg()                                          # una credencial = una sesión (409) sigue igual

    def test_persist_false_no_escribe_archivo(self):
        eng, _ = sim_engine(sessions={"persist": False})
        self.assertIsNone(eng.sessions.store)
        self.assertFalse(os.path.exists(os.path.join(eng.cfg["data_dir"], "sessions_state.json")))


class CaidaTest(unittest.TestCase):
    def test_caida_brusca_justo_despues_de_marcar_en_curso(self):
        d = tempfile.mkdtemp(prefix="cauce-ses-")
        code = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
from cauce.audit import AuditLog
from cauce.session_store import SessionStore
from cauce.sessions import SessionRegistry
r = SessionRegistry(AuditLog(os.path.join({d!r}, 'audit.jsonl')), lambda c: 'ok', lambda c: True,
                    store=SessionStore(os.path.join({d!r}, 'sessions_state.json')))
s = r.register('tok-a', 'forms', {{'label': 'forms-a', 'authorized_by_user': True}})
r.heartbeat(s.id, 'tok-a', {{'status': 'ok', 'operation': 'in_progress'}})
print(s.id, flush=True)
os.kill(os.getpid(), 9)
"""
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0, "el proceso debe morir por la señal, sin apagado ordenado")
        sid = p.stdout.strip()
        h = Reg(d)
        s = h.r.all()[0]
        self.assertEqual((s.id, s.requires_human, s.human_reason), (sid, True, "engine_restart_during_operation"))

    def test_escritura_cortada_deja_temporal_y_no_dana_el_principal(self):
        h = Reg()
        s = h.reg()
        with open(h.path + ".999.dead.tmp", "w") as fh:     # restos de una escritura interrumpida
            fh.write('{"schema": "cauce.sess')
        self.assertEqual(h.restart().all()[0].id, s.id)


class ArchivoDanadoTest(unittest.TestCase):
    def test_principal_danado_usa_respaldo_y_aparta_el_danado(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")                # segunda escritura: el anterior pasa a .bak
        with open(h.path, "w") as fh:
            fh.write('{"schema": "cauce.sessions-state/1", "trunc')
        r = h.restart()
        self.assertEqual(r.all()[0].id, s.id)
        self.assertIn("session.state_recovered_from_backup", h.kinds())
        self.assertTrue(any(".corrupt-" in f for f in os.listdir(h.d)), "el dañado se aparta, no se borra")

    def test_alterado_a_mano_se_detecta_por_el_resumen(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.restart()                                          # queda requires_human guardado
        doc = json.load(open(h.path))
        doc["sessions"][0]["requires_human"] = False         # alguien intenta «autorizarla» editando el archivo
        json.dump(doc, open(h.path, "w"))
        r = h.restart()
        self.assertTrue(r.all()[0].requires_human, "la edición no pasa el resumen SHA-256: se usa el respaldo")

    def test_principal_y_respaldo_danados_empieza_vacio_y_lo_audita(self):
        h = Reg()
        h.reg()
        for p in (h.path, h.path + ".bak"):
            with open(p, "w") as fh:
                fh.write("basura")
        r = h.restart()
        self.assertEqual(r.all(), [])
        self.assertIn("session.state_corrupt", h.kinds())

    def test_campos_invalidos_se_descartan(self):
        h = Reg()
        st = SessionStore(h.path)
        st.save([{"id": "bs_x", "label": "x"}, "no-dict"], [], 1.0)
        self.assertEqual(h.restart().all(), [])


class FalloEscrituraTest(unittest.TestCase):
    def test_fallo_de_escritura_no_detiene_el_motor_y_se_audita_una_vez(self):
        class Roto(SessionStore):
            fail = True

            def save(self, *a, **kw):
                if Roto.fail:
                    raise OSError(28, "No space left on device")
                return super().save(*a, **kw)

        h = Reg()
        r = h.restart(Roto(h.path))
        s = h.reg()
        for _ in range(5):
            h.beat(s.id, operation="in_progress" if _ % 2 else "idle")
        self.assertEqual(h.kinds().count("session.state_write_failed"), 1)
        self.assertEqual(r.all()[0].id, s.id, "sigue funcionando en memoria")
        Roto.fail = False
        h.beat(s.id, operation="in_progress")
        self.assertIn("session.state_saved", h.kinds())
        self.assertEqual(h.restart().all()[0].human_reason, "engine_restart_during_operation")


class SuspensionYCaducidadTest(unittest.TestCase):
    def test_suspension_corta_deja_incierta_y_con_operacion_pide_persona(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(600)                                 # el equipo se suspendió 10 min
        h.r.evaluate()
        v = h.r.view(h.r.all()[0])
        self.assertEqual((v["state"], v["requires_human"]), ("stale", True))
        h.beat(s.id, operation="idle")
        self.assertTrue(h.r.all()[0].requires_human)

    def test_suspension_larga_caduca_sin_perder_la_marca(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(5 * 3600)
        h.r.evaluate()
        self.assertEqual(h.r.all(), [])
        c = h.r.summary()["closed"][0]
        self.assertEqual((c["id"], c["reason"], c["requires_human"]), (s.id, "expired", True))
        self.assertIn("session.expired", h.kinds())

    def test_reinicio_tras_mucho_tiempo_caduca(self):
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="in_progress")
        h.clock.advance(2 * 3600)                            # motor apagado 2 h
        r = h.restart()
        self.assertEqual(r.all(), [])
        c = r.summary()["closed"][0]
        self.assertEqual((c["id"], c["reason"], c["requires_human"]), (s.id, "expired_after_restart", True))


def _escritor(path, n):
    st = SessionStore(path)
    for i in range(n):
        st.save([], [{"id": f"c{os.getpid()}-{i}", "closed_at": float(i)}], float(i))


class ConcurrenciaTest(unittest.TestCase):
    def test_reinicios_y_escrituras_simultaneas_nunca_dejan_un_archivo_ilegible(self):
        d = tempfile.mkdtemp(prefix="cauce-ses-")
        path = os.path.join(d, "sessions_state.json")
        ctx = multiprocessing.get_context("spawn")
        ps = [ctx.Process(target=_escritor, args=(path, 40)) for _ in range(4)]
        for p in ps:
            p.start()
        for p in ps:
            p.join(60)
        self.assertTrue(all(p.exitcode == 0 for p in ps), [p.exitcode for p in ps])
        doc, status = SessionStore(path).load()
        self.assertIn(status, ("ok", "backup"))
        self.assertIsNotNone(doc)
        self.assertFalse([f for f in os.listdir(d) if f.endswith(".tmp")], "no quedan temporales")


class MotorTest(SessionApiBase):
    def test_reinicio_del_motor_por_la_api_y_sin_secretos_en_disco(self):
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        self.a.heartbeat(True, operation="in_progress")
        path = os.path.join(self.eng.cfg["data_dir"], "sessions_state.json")
        raw = open(path, encoding="utf-8").read()
        for secreto in (self.tok_a["token"], self.api.token, self.read_token):
            self.assertNotIn(secreto, raw)
        for palabra in ("cookie", "url", "password", "contrase"):
            self.assertNotIn(palabra, raw.lower())
        doc = json.loads(raw)
        self.assertEqual(set(doc), {"schema", "saved_at", "sessions", "closed", "sha256"})
        st = SessionStore(path)
        r2 = SessionRegistry(self.eng.audit, lambda c: "ok", lambda c: True, store=st, clock=self.clock)   # «nuevo motor»
        s = r2.all()[0]
        self.assertEqual((s.id, s.requires_human, s.restored), (self.a.session_id, True, True))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()


class EventosPublicadosTest(SessionApiBase):
    def test_eventos_de_e4_y_e5_llegan_a_la_lectura_de_monitor(self):
        """Antes de E5 /status/events filtraba estos tipos: Monitor no podía mostrarlos."""
        self.a.register("forms-a", connection_id="sim-a", heartbeat_timeout_s=10)
        self.clock.advance(1)
        self.a.heartbeat(True, operation="in_progress")
        self.fail_link("sim-a")
        self.admin._call("POST", f"/sessions/{self.a.session_id}/resolve")
        self.eng.check_resume(10_000.0, 1_000.0)
        raw = self.raw("GET", "/api/v1/status/events?since=0&limit=500", self.read_token)[1]
        kinds = [e["kind"] for e in json.loads(raw)["events"]]
        for k in ("session.requires_human", "session.human_resolved", "system.resume_detected"):
            self.assertIn(k, kinds)
        self.assertNotIn(self.tok_a["token"], raw)
        self.assertNotIn("owner", raw)
