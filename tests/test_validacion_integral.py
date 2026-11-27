"""Validación integral (scripts/laboratorio_integrado/integral.py): reglas que no dependen de Windows ni de los motores."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts" / "laboratorio_integrado"))
import integral as I  # noqa: E402
import lab  # noqa: E402


def nuevo(d, perfil="rapido", interactivo=False):
    lab.validar_carpeta(Path(d) / "l", crear=True)
    return I.Integral(lab.Lab(Path(d) / "l"), perfil, interactivo, None, preguntar=lambda _m: "")


class ReglasTest(unittest.TestCase):
    def test_perfiles_y_duraciones(self):
        self.assertEqual(I.PERFILES["prolongado"]["estabilidad_s"], 3600)
        self.assertEqual(I.PERFILES["nocturno"]["estabilidad_s"], 8 * 3600)
        self.assertFalse(I.PERFILES["prolongado"]["fisica"], "las pruebas largas no generan tráfico a terceros")
        self.assertFalse(I.PERFILES["nocturno"]["fisica"])
        self.assertEqual(I.OBJETIVO_POR_OMISION, "github.com:443")

    def test_un_paso_que_revienta_no_detiene_los_demas(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d)
            r = it.paso("x", "rompe", lambda: 1 / 0)
            self.assertEqual(r["estado"], I.FALLA)
            self.assertIn("ZeroDivisionError", r["detalle"])
            r2 = it.paso("y", "sigue", lambda: {"estado": I.PASA})
            self.assertEqual(r2["estado"], I.PASA)
            self.assertTrue((it.dir / "INFORME_VALIDACION_INTEGRAL.md").exists(), "informe parcial tras cada paso")

    def test_la_matriz_prefiere_evidencia_fisica_y_no_borra_un_fallo_fisico(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d)
            it.mf("F6", "Aprobado en simulación", "s", "sim")
            it.mf("F6", "Aprobado físicamente", "f", "real")
            it.mf("F6", "Aprobado en simulación", "s2", "sim")
            self.assertEqual(it.matriz["F6"]["estado"], "Aprobado físicamente")
            it.mf("F7", "Fallido", "corte real", "real")
            it.mf("F7", "Aprobado en simulación", "s", "sim")
            self.assertEqual(it.matriz["F7"]["estado"], "Fallido")
            it.escribir()
            md = (it.dir / "INFORME_VALIDACION_INTEGRAL.md").read_text("utf-8")
            self.assertIn("| F4 | Pendiente |", md, "lo no medido nunca aparece aprobado")

    def test_sin_persona_lo_fisico_queda_pendiente(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d, "completo", interactivo=False)
            with mock.patch.object(it, "_telefonos", return_value=[]):
                r = it.p06_telefono()
            self.assertEqual(r["estado"], I.PENDIENTE)
            self.assertTrue(it.intervenciones)

    def test_fuga_solo_se_juzga_con_datos_suficientes(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d)
            it._errores = lambda: {"network_engine": 0, "monitor_trazas": 0, "ultima_traza": None}
            corta = [{"t": t, "procesos": {"m": {"vivo": True, "ram_mb": 100 + t / 10, "cpu_s": t / 100}}} for t in range(0, 600, 60)]
            r = it._resumen_estabilidad(corta, [], 0, {}, {}, 600)
            self.assertEqual(r["procesos"]["m"]["fuga"], "sin datos suficientes (< 30 min)")
            larga = [{"t": t, "procesos": {"m": {"vivo": True, "ram_mb": 100 + t / 60, "cpu_s": 1}}} for t in range(0, 7200, 60)]
            r = it._resumen_estabilidad(larga, [], 0, {}, {}, 7200)
            self.assertTrue(r["procesos"]["m"]["fuga"])
            self.assertTrue(any("fuga" in p for p in r["problemas"]))
            plana = [{"t": t, "procesos": {"m": {"vivo": True, "ram_mb": 100.0, "cpu_s": 1}}} for t in range(0, 7200, 60)]
            self.assertFalse(it._resumen_estabilidad(plana, [], 0, {}, {}, 7200)["procesos"]["m"]["fuga"])

    def test_solo_cuentan_las_trazas_nuevas_durante_la_supervision(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d)
            logs = it.lab.d / "datos" / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            traza = "Traceback (most recent call last):\n  File \"x.py\", line 1\nConnectionResetError: [WinError 10054]\n"
            (logs / "monitor.log").write_text(traza, "utf-8")
            err0 = it._errores()
            self.assertEqual(err0["monitor_trazas"], 1)
            self.assertIn("ConnectionResetError", err0["ultima_traza"])
            m = [{"t": 0, "procesos": {}}]
            r = it._resumen_estabilidad(m, [], 0, {}, {}, 60, err0)
            self.assertFalse(r["problemas"], "una traza de un paso anterior no hace fallar la estabilidad")
            self.assertEqual(r["errores"]["previos"]["monitor_trazas"], 1, "pero se informa")
            (logs / "monitor.log").write_text(traza * 2, "utf-8")
            r = it._resumen_estabilidad(m, [], 0, {}, {}, 60, err0)
            self.assertTrue(any("durante la supervisión" in p for p in r["problemas"]))

    def test_detecta_credenciales_y_mac_en_los_informes(self):
        with tempfile.TemporaryDirectory() as d:
            it = nuevo(d)
            it.lab.estado["monitor_admin"] = "clave-ficticia-123"
            (it.dir / "x.md").write_text("ok", "utf-8")
            self.assertEqual(it.p16_datos_sensibles()["estado"], I.PASA)
            (it.dir / "y.log").write_text("usuario admin clave-ficticia-123 mac 00-11-22-33-44-55", "utf-8")
            r = it.p16_datos_sensibles()
            self.assertEqual(r["estado"], I.FALLA)
            self.assertIn("credencial", r["detalle"])
            self.assertIn("MAC", r["detalle"])

    def test_lanzador_integral_ascii_crlf_y_perfiles(self):
        f = RAIZ / "scripts" / "laboratorio_integrado" / "7_VALIDACION_INTEGRAL.cmd"
        b = f.read_bytes()
        b.decode("ascii")
        self.assertNotIn(b"\n", b.replace(b"\r\n", b""))
        t = b.decode("ascii")
        for p in ("rapido", "completo", "prolongado", "nocturno", "integral.py", "--navegador-forms"):
            self.assertIn(p, t)


if __name__ == "__main__":
    unittest.main()
