"""E8 (RC3): concurrencia y persistencia de estados críticos.

Propiedad central: ninguna escritura vieja puede dejar en disco un estado anterior a uno más nuevo ya escrito, y una marca
«revisar a mano» que llegó al disco no desaparece del disco salvo resolución explícita de una persona administradora."""
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from test_sesiones_persistencia import ROOT, Reg
import cauce.sessions as _sesiones
from cauce.session_store import SessionStore
from cauce.sessions import SessionRegistry


class RegN(Reg):
    """Como Reg, con hasta 8 sesiones (el máximo admitido)."""

    def _new(self, store=None):
        return SessionRegistry(self.audit, lambda cid: self.net["v"], lambda cid: True, clock=self.clock,
                               store=store or SessionStore(self.path), restore_max_age_s=self.max_age, max_sessions=8)


class SinLimiteDeLatido:
    """Aquí se estudian cruces de escrituras, no el límite de frecuencia de latidos (que tiene sus propias pruebas)."""

    def setUp(self):
        self._min = _sesiones.MIN_HEARTBEAT_INTERVAL_S
        _sesiones.MIN_HEARTBEAT_INTERVAL_S = 0

    def tearDown(self):
        _sesiones.MIN_HEARTBEAT_INTERVAL_S = self._min


class _Espia:
    """Envuelve SessionStore._save: retrasa cada escritura al azar (para forzar cruces entre hilos) y anota, en el orden
    REAL en que quedan en disco, las fotos escritas."""

    def __init__(self, semilla=7, max_ms=4):
        self.rnd = random.Random(semilla)
        self.max_ms = max_ms
        self.escritas = []           # (marcas_en_la_foto, operaciones) en orden de escritura
        self.lk = threading.Lock()
        self.real = SessionStore._save

    def __enter__(self):
        espia = self

        def _save(self_, sessions, closed, saved_at, pending):
            with espia.lk:
                d = espia.rnd.random() * espia.max_ms / 1000
            time.sleep(d)
            espia.real(self_, sessions, closed, saved_at, pending)
            marcas = frozenset(s["id"] for s in sessions if s.get("requires_human"))
            marcas |= frozenset(p.get("session_id") for p in pending)
            with espia.lk:
                espia.escritas.append(marcas)

        SessionStore._save = _save
        return self

    def __exit__(self, *a):
        SessionStore._save = self.real


class EscriturasCruzadasTest(SinLimiteDeLatido, unittest.TestCase):
    def _jornada_concurrente(self, r, hilos=8, vueltas=25):
        """Cada hilo tiene su sesión: late, a veces deja una operación en curso «colgada» (la marca), y otro hilo fuerza
        guardados todo el tiempo (como el bucle del motor)."""
        sesiones = [r.reg(f"tok-{i}", f"forms-{i}") for i in range(hilos)]
        parar = threading.Event()
        errores = []

        def guardador():
            while not parar.is_set():
                r.r._save(force=True)

        def trabajador(i):
            s = sesiones[i]
            try:
                for v in range(vueltas):
                    op = "in_progress" if v % 3 else "idle"
                    r.r.heartbeat(s.id, f"tok-{i}", {"status": "ok", "operation": op})
                # la última operación queda en curso y se informa un fallo a medias: marca «revisar a mano»
                r.r.heartbeat(s.id, f"tok-{i}", {"status": "error", "error_code": "network_error",
                                                 "operation": "in_progress"})
            except Exception as e:  # noqa: BLE001
                errores.append(repr(e))

        g = [threading.Thread(target=guardador) for _ in range(2)]
        t = [threading.Thread(target=trabajador, args=(i,)) for i in range(hilos)]
        for x in g + t:
            x.start()
        for x in t:
            x.join(60)
        parar.set()
        for x in g:
            x.join(60)
        self.assertEqual(errores, [])
        return sesiones

    def test_ninguna_foto_vieja_quita_del_disco_una_marca_ya_escrita(self):
        r = RegN()
        with _Espia() as esp:
            sesiones = self._jornada_concurrente(r)
            r.r._save(force=True)
        # en el orden real de escritura, una marca que llegó al disco nunca desaparece después
        vistas = set()
        for i, marcas in enumerate(esp.escritas):
            perdidas = vistas - marcas
            self.assertFalse(perdidas, f"la escritura n.º {i} quitó del disco marcas ya escritas: {sorted(perdidas)}")
            vistas |= marcas
        self.assertGreater(len(esp.escritas), 20, "la prueba debe producir muchas escrituras cruzadas")
        # apagado brusco tras la jornada: todas las marcas siguen ahí
        r2 = r.restart()
        marcadas = {s.id for s in r2.all() if s.requires_human} | {p["session_id"] for p in r2._pending}
        self.assertEqual(marcadas, {s.id for s in sesiones})

    def test_el_disco_queda_en_la_foto_mas_nueva(self):
        r = RegN()
        with _Espia(semilla=11):
            self._jornada_concurrente(r, hilos=6, vueltas=15)
            ultima = {s.id: (s.operation, s.requires_human) for s in r.r.all()}
            r.r._save(force=True)
        doc, status = SessionStore(r.path).load()
        self.assertEqual(status, "ok")
        en_disco = {s["id"]: (s["operation"], s["requires_human"]) for s in doc["sessions"]}
        self.assertEqual(en_disco, ultima)
        self.assertFalse([f for f in os.listdir(r.d) if f.endswith(".tmp")], "no quedan temporales")

    def test_una_foto_descartada_no_deja_el_estado_sin_guardar(self):
        """Si la foto nueva se escribe y la vieja se descarta, nada queda pendiente: el disco ya tiene lo nuevo."""
        r = Reg()
        s = r.reg()
        r.beat(s.id, operation="in_progress")
        st = r.r.store
        self.assertFalse(st.save([], [], 0.0, [], seq=1), "una foto con número ya superado debe descartarse")
        doc, _ = st.load()
        self.assertEqual(doc["sessions"][0]["operation"], "in_progress")


class ApiConcurrenteTest(unittest.TestCase):
    """Latidos por HTTP desde varios hilos + resoluciones admin a la vez: el archivo siempre legible, la auditoría
    encadenada y cada marca resuelta una sola vez."""

    def setUp(self):
        from helpers import sim_engine
        from cauce.api import CauceAPI
        from cauce.client import CauceClient
        self.eng, _ = sim_engine(sessions={"max": 8})
        self.api = CauceAPI(self.eng, "127.0.0.1", 0)
        self.api.start()
        self.adm = CauceClient(self.api.token, port=self.api.port, timeout=10)

    def tearDown(self):
        self.api.stop()

    def test_latidos_y_resoluciones_simultaneas(self):
        from cauce.client import CauceSessionClient
        clientes = []
        for i in range(4):
            t = self.adm.create_token(f"forms-{i}", "session")
            c = CauceSessionClient(t["token"], port=self.api.port, timeout=10)
            c.register(f"forms-{i}")
            clientes.append(c)
        time.sleep(0.6)
        # cada sesión deja una marca y se cierra: quedan 4 marcas sin sesión
        for c in clientes:
            c.heartbeat(ok=False, error_code="network_error", operation="in_progress")
            c.close()
        pend = self.adm.pending_reviews()
        self.assertEqual(len(pend), 4)
        resultados, lk = [], threading.Lock()

        def resolver(p):
            for _ in range(3):                    # cada marca se intenta resolver tres veces a la vez
                try:
                    self.adm.resolve_pending(p["id"], "revisado en el portal, sin envío", p["confirm_with"])
                    with lk:
                        resultados.append(("ok", p["id"]))
                except Exception as e:  # noqa: BLE001
                    with lk:
                        resultados.append((getattr(e, "status", None), p["id"]))

        hs = [threading.Thread(target=resolver, args=(p,)) for p in pend for _ in range(2)]
        for h in hs:
            h.start()
        for h in hs:
            h.join(60)
        oks = [x for x in resultados if x[0] == "ok"]
        self.assertEqual(sorted(i for _, i in oks), sorted(p["id"] for p in pend), resultados)
        self.assertTrue(all(x[0] in ("ok", 404) for x in resultados), resultados)
        self.assertEqual(self.adm.pending_reviews(), [])
        doc, status = self.eng.sessions.store.load()
        self.assertEqual((status, doc["pending"]), ("ok", []))
        ev = [e for e in self.eng.audit.events(limit=5000) if e["kind"] == "session.pending_resolved"]
        self.assertEqual(len(ev), 4)
        self.assertTrue(all(e["data"].get("note") and e["data"].get("resolved_by") for e in ev))
        seqs = [e["seq"] for e in self.eng.audit.events(limit=5000)]
        self.assertEqual(seqs, sorted(set(seqs)), "la auditoría no repite ni desordena números")


_HIJO = """
import os, sys, threading
sys.path.insert(0, {root!r})
from cauce.audit import AuditLog
from cauce.session_store import SessionStore
import cauce.sessions as m
from cauce.sessions import SessionRegistry
m.MIN_HEARTBEAT_INTERVAL_S = 0     # se estudia la muerte brusca, no el límite de frecuencia
d = {d!r}
r = SessionRegistry(AuditLog(os.path.join(d, 'audit.jsonl')), lambda c: 'ok', lambda c: True,
                    store=SessionStore(os.path.join(d, 'sessions_state.json')))
s = r.register('tok-a', 'forms', {{'label': 'forms-a', 'authorized_by_user': True}})
print('sid', s.id, flush=True)
def guardar():
    while True:
        r._save(force=True)
threading.Thread(target=guardar, daemon=True).start()
i = 0
while True:
    op = 'in_progress' if i % 2 else 'idle'
    r.heartbeat(s.id, 'tok-a', {{'status': 'ok', 'operation': op}})
    print('ack', op, flush=True)
    i += 1
"""


class MuerteBruscaTest(unittest.TestCase):
    def test_matar_el_proceso_en_momentos_al_azar_nunca_pierde_una_operacion_confirmada(self):
        """El proceso se mata con SIGKILL en momentos al azar mientras late y otro hilo guarda. Si el último latido
        CONFIRMADO dejó la operación en curso, tras reiniciar se exige revisión humana. Nunca se lee un archivo roto."""
        rnd = random.Random(3)
        casos = []
        for n in range(12):
            d = tempfile.mkdtemp(prefix="cauce-e8-kill-")
            p = subprocess.Popen([sys.executable, "-c", _HIJO.format(root=ROOT, d=d)], stdout=subprocess.PIPE,
                                 text=True)
            sid = p.stdout.readline().split()[1]
            tope = time.time() + 0.05 + rnd.random() * 0.4
            ultimo = None
            while time.time() < tope:
                linea = p.stdout.readline()
                if linea.startswith("ack"):
                    ultimo = linea.split()[1]
            p.kill()
            # lo que el hijo llegó a confirmar antes de morir y aún estaba en el tubo también cuenta
            for linea in p.stdout.read().splitlines():
                if linea.startswith("ack"):
                    ultimo = linea.split()[1]
            p.wait(30)
            p.stdout.close()
            doc, status = SessionStore(os.path.join(d, "sessions_state.json")).load()
            # E8: el respaldo se copia (no se mueve), así que el principal existe y es legible en todo momento
            self.assertEqual(status, "ok", f"vuelta {n}: el principal no estaba o no se podía leer")
            h = Reg(d)
            s = {x.id: x for x in h.r.all()}.get(sid)
            casos.append((ultimo, status, s.requires_human if s else None))
            if ultimo == "in_progress":
                self.assertTrue(s is not None and s.requires_human, f"vuelta {n}: se perdió la operación en curso")
            if status == "backup":
                self.assertTrue(s is not None and s.requires_human, f"vuelta {n}: respaldo sin pedir revisión")
        self.assertTrue(any(c[0] == "in_progress" for c in casos), casos)
        if os.environ.get("CAUCE_E8_VERBOSO"):
            print(casos)


class ArchivoDanadoTest(unittest.TestCase):
    def test_cada_forma_de_dano_conserva_o_exige_revision(self):
        """Truncado, vacío, JSON válido con resumen falso, campo de otro tipo, archivo enorme: nunca se arranca como si
        no hubiera pasado nada."""
        danos = {
            "truncado": lambda b: b[: len(b) // 2],
            "vacio": lambda b: b"",
            "resumen_falso": lambda b: json.dumps({**json.loads(b), "sha256": "0" * 64}).encode(),
            "tipo_cambiado": lambda b: json.dumps({**json.loads(b), "sessions": "x"}).encode(),
            "enorme": lambda b: b + b" " * (5 * 1024 * 1024),
        }
        for nombre, danar in danos.items():
            with self.subTest(nombre):
                h = Reg()
                s = h.reg()
                h.beat(s.id, operation="idle")
                h.beat(s.id, operation="in_progress")
                with open(h.path, "rb") as fh:
                    bueno = fh.read()
                with open(h.path, "wb") as fh:
                    fh.write(danar(bueno))
                r2 = h.restart()
                v = {x.id: x for x in r2.all()}.get(s.id)
                generales = [p for p in r2._pending if p["owner_token_id"] is None]
                self.assertTrue((v is not None and v.requires_human) or generales,
                                f"{nombre}: arrancó sin exigir revisión")
                self.assertTrue([f for f in os.listdir(h.d) if ".corrupt-" in f], f"{nombre}: el dañado no se apartó")


if __name__ == "__main__":
    unittest.main()
