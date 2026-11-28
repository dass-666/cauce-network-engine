"""E8b: RC2 y RC3 en el mismo equipo sin mezclarse (scripts/candidatas). Lo que no depende de Windows."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts" / "candidatas"))
import candidatas as C  # noqa: E402


class CandidatasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"USERPROFILE": str(self.home)})
        self.env.start()
        for k in ("CAUCE_RC2_NE", "CAUCE_RC2_LAB", "CAUCE_RC3_LAB"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_carpetas_y_puertos_separados(self):
        self.assertNotEqual(C.rc2_lab(), C.rc3_lab())
        self.assertEqual(C.PUERTOS_RC3, (8200, 8965, 8980, 8970))
        self.assertFalse(set(C.PUERTOS_RC2) & set(C.PUERTOS_RC3))
        self.assertEqual(C.rc2_ne(), self.home / "Cauce-AI-Center" / "cauce-lab-e3" / "ne-src")

    def test_commit_esperado_distinto_se_niega(self):
        err = C.comprobar("6e2cceb")
        self.assertTrue(any("se esperaba 6e2cceb" in e for e in err), err)

    def test_no_se_instala_sobre_el_clon_de_rc2(self):
        with mock.patch.dict(os.environ, {"CAUCE_RC2_NE": str(C.RAIZ)}):
            self.assertTrue(any("clon de RC2" in e for e in C.comprobar()))

    def test_laboratorio_rc3_no_puede_ser_el_de_rc2(self):
        with mock.patch.dict(os.environ, {"CAUCE_RC3_LAB": str(C.rc2_lab() / "sub")}):
            self.assertTrue(any("no puede vivir dentro" in e for e in C.comprobar()))

    def test_versiones_deben_ser_las_de_rc3(self):
        with mock.patch.dict(C.RC3, {"monitor": "0" * 40}):
            self.assertTrue(any("versiones.json fija monitor" in e for e in C.comprobar()))

    def test_nunca_a_la_vez(self):
        lab2 = C.rc2_lab()
        lab2.mkdir(parents=True)
        (lab2 / ".en_uso").write_text(json.dumps({"pid": os.getpid(), "quien": "integral completo"}))
        self.assertTrue(any("en uso" in e for e in C.otra_parada("rc3")))
        (lab2 / ".en_uso").write_text(json.dumps({"pid": 999999, "quien": "cortada"}))   # proceso que ya no existe
        with mock.patch.object(C, "puerto_ocupado", return_value=False):
            self.assertEqual(C.otra_parada("rc3"), [])
        with mock.patch.object(C, "puerto_ocupado", side_effect=lambda p: p == 8980):
            self.assertTrue(any("8980" in e for e in C.otra_parada("rc2")))

    def test_huella_rc2_solo_lee(self):
        lab2 = C.rc2_lab()
        lab2.mkdir(parents=True)
        (lab2 / "estado.json").write_text(json.dumps({"commits": {"network_engine": "6e2", "monitor": "967", "forms": "aa1"}}))
        antes = {p: p.stat().st_mtime_ns for p in lab2.rglob("*")}
        h = C.huella_rc2()
        self.assertEqual(h["commits_instalados"]["monitor"], "967")
        self.assertEqual(antes, {p: p.stat().st_mtime_ns for p in lab2.rglob("*")})

    def test_comparar_mismo_perfil(self):
        def informe(lab, estados, commit):
            d = lab / "informes" / "integral-20261128-010000"
            d.mkdir(parents=True)
            pasos = [{"id": f"{i:02d}", "titulo": f"Paso {i}", "estado": e, "evidencia": "SIMULADA", "s": 1.0}
                     for i, e in enumerate(estados, 1)]
            pasos[0]["versiones"] = [{"componente": "network_engine", "commit": commit}]
            (d / "validacion_integral.json").write_text(json.dumps({"perfil": "completo", "pasos": pasos, "matriz": {}}))
        informe(C.rc2_lab(), ["PASA", "PASA", "PENDIENTE"], "6e2cceb0")
        informe(C.rc3_lab(), ["PASA", "FALLA", "PENDIENTE"], "d961c0d0")
        out = self.home / "comp.md"
        self.assertEqual(C.comparar(out), 0)
        t = out.read_text("utf-8")
        self.assertIn("Pasos con resultado distinto: **1**", t)
        self.assertIn("| 02 | Paso 2 | PASA", t)

    def test_lanzadores_ascii_crlf(self):
        for f in (RAIZ / "scripts" / "candidatas").glob("*.cmd"):
            b = f.read_bytes()
            b.decode("ascii")
            self.assertNotIn(b"\n", b.replace(b"\r\n", b""), f.name)


if __name__ == "__main__":
    unittest.main()
