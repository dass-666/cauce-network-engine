"""E10: instalación independiente (sin tocar RC2/RC3) y restauración segura. Linux; sin red ni navegador."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "scripts" / "e10"))
import e10  # noqa: E402


class InstaladorE10Test(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="e10-"))
        self.env = mock.patch.dict(os.environ, {"CAUCE_E10_LAB": str(self.d / "lab"),
                                                "CAUCE_E10_RESPALDOS": str(self.d / "resp"),
                                                "CAUCE_RC2_LAB": str(self.d / "rc2lab"), "CAUCE_RC3_LAB": str(self.d / "rc3lab"),
                                                "CAUCE_RC2_NE": str(self.d / "rc2ne"), "CAUCE_RC3_NE": str(self.d / "rc3ne")})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_puertos_y_combinacion_propios(self):
        self.assertEqual(e10.PUERTOS, (8300, 9065, 9080, 9070))
        self.assertTrue(set(e10.PUERTOS).isdisjoint(set(e10.C.PUERTOS_RC2) | set(e10.C.PUERTOS_RC3)))
        v = json.loads(e10.VERSIONES.read_text("utf-8"))
        self.assertEqual(v["forms"]["commit"], "e55a45d99b34b10d6f477cda9dfe0a851dfc8b63")
        rc3 = json.loads((RAIZ / "scripts" / "laboratorio_integrado" / "versiones.json").read_text("utf-8"))
        self.assertEqual(rc3["forms"]["commit"], e10.C.RC3["forms"], "versiones.json sigue en RC3")
        self.assertEqual(rc3["monitor"]["commit"], e10.C.RC3["monitor"])
        env = e10.entorno()
        self.assertEqual((env["CAUCE_LAB_DESPLAZAMIENTO_PUERTOS"], env["CAUCE_LAB_09B_EXIGIR_NATIVO"]), ("200", "1"))

    def test_no_puede_vivir_dentro_de_rc2_o_rc3(self):
        with mock.patch.dict(os.environ, {"CAUCE_E10_LAB": str(self.d / "rc3lab" / "e10")}):
            self.assertTrue(any("RC3" in e or "rc3lab" in e for e in e10.comprobar()))

    def _datos(self):
        dd = self.d / "lab" / "datos"
        (dd / "forms" / "estado").mkdir(parents=True)
        (dd / "network-engine").mkdir(parents=True)
        (dd / "forms" / "estado" / "operaciones.json").write_text("VIEJO", "utf-8")
        (dd / "network-engine" / "sessions_state.json").write_text("SESIONES-VIEJAS", "utf-8")
        (dd / "monitor.token").write_text("tok", "utf-8")
        return dd

    def test_restaurar_no_retrocede_forms_y_el_motor_pide_revision(self):
        dd = self._datos()
        z = e10.respaldar(silencioso=True)
        (dd / "forms" / "estado" / "operaciones.json").write_text("NUEVO-CON-MAS-ENVIOS", "utf-8")
        (dd / "monitor.token").write_text("cambiado", "utf-8")
        self.assertEqual(e10.restaurar(z), 0)
        self.assertEqual((dd / "forms" / "estado" / "operaciones.json").read_text("utf-8"), "NUEVO-CON-MAS-ENVIOS")
        self.assertEqual((dd / "monitor.token").read_text("utf-8"), "tok")
        ne = dd / "network-engine"
        self.assertFalse((ne / "sessions_state.json").exists())
        self.assertEqual((ne / "sessions_state.json.bak").read_text("utf-8"), "SESIONES-VIEJAS")
        aparte = [x for x in (self.d / "lab").iterdir() if x.name.startswith("datos.antes-de-restaurar-")]
        self.assertEqual(len(aparte), 1, "lo actual se aparta, nunca se borra")

    def test_el_navegador_no_viaja_en_el_respaldo_y_se_conserva_al_restaurar(self):
        dd = self._datos()
        nav = dd / e10.NAVEGADORES / "chromium-1" / "chrome.exe"
        nav.parent.mkdir(parents=True)
        nav.write_bytes(b"x" * 1024)
        z = e10.respaldar(silencioso=True)
        import zipfile
        with zipfile.ZipFile(z) as zf:
            self.assertFalse([n for n in zf.namelist() if n.startswith(e10.NAVEGADORES)])
        self.assertEqual(e10.restaurar(z), 0)
        self.assertTrue(nav.exists(), "las jornadas usan datos/ms-playwright: debe seguir tras restaurar")

    def test_restaurar_exige_huella_correcta(self):
        self._datos()
        z = Path(e10.respaldar(silencioso=True))
        with open(z, "ab") as fh:
            fh.write(b"x")
        self.assertEqual(e10.restaurar(str(z)), 2)

    def test_el_motor_real_trata_el_estado_restaurado_como_copia_anterior(self):
        """Lo que deja restaurar (solo .bak) hace que el motor arranque con marca general y revisión de todas."""
        sys.path.insert(0, str(RAIZ / "tests"))
        from test_sesiones_persistencia import Reg
        h = Reg()
        s = h.reg()
        h.beat(s.id, operation="idle")
        os.replace(h.path, h.path + ".bak")
        r = h.restart()
        self.assertTrue(all(x.requires_human for x in r.all()))
        self.assertTrue(any(p["reason"] == "state_recovered_from_backup" for p in r.pending()))


if __name__ == "__main__":
    unittest.main()
