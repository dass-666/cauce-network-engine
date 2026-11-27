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
        for p in ("rapido", "completo", "prolongado", "nocturno", "integral.py", "--navegador-forms", "al-dia"):
            self.assertIn(p, t)

    def test_una_instalacion_vieja_se_detecta_para_reinstalar(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            self.assertEqual(lb.desactualizado(), ["sin instalar"])
            (lb.ne / "cauce").mkdir(parents=True)
            lb.monitor.mkdir()
            lb.forms.mkdir()
            lb.estado.update({"instalado": lab.ahora(), "commits": {"network_engine": "sin git", "monitor": lab.VERSIONES["monitor"]["commit"],
                                          "forms": lab.VERSIONES["forms"]["commit"]}, "forms": True})
            lb.guardar()
            self.assertEqual(lb.desactualizado(), [])
            lb.estado["commits"]["monitor"] = "a" * 40
            lb.guardar()
            self.assertTrue(any("monitor" in m for m in lb.desactualizado()))
            self.assertEqual(lab.main(["--dir", str(lb.d), "al-dia"]), 3)
            lb.estado["commits"]["monitor"] = lab.VERSIONES["monitor"]["commit"]
            lb.estado["instalacion_en_curso"] = lab.ahora()      # E5: una instalación cortada a medias
            lb.guardar()
            self.assertIn("una instalación anterior no terminó", lb.desactualizado())


class RobustezLanzadorTest(unittest.TestCase):
    """E5: comprobación previa, bloqueo, restauración tras una actualización fallida y cancelación segura."""

    def test_bloqueo_impide_dos_ejecuciones_y_se_libera(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with lab.Bloqueo(d, "primera"):
                with self.assertRaises(lab.Ocupado):
                    with lab.Bloqueo(d, "segunda"):
                        pass
            self.assertFalse((d / ".en_uso").exists())
            with lab.Bloqueo(d, "tercera"):
                pass

    def test_bloqueo_huerfano_se_sustituye(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / ".en_uso").write_text('{"pid": 999999999, "quien": "cortada"}', "utf-8")
            with lab.Bloqueo(d, "nueva"):
                self.assertIn('"nueva"', (d / ".en_uso").read_text("utf-8"))

    def test_actualizacion_fallida_restaura_la_anterior(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            for x in (lb.ne, lb.monitor, lb.forms):
                x.mkdir()
                (x / "VIEJO").write_text("v", "utf-8")
            lb.estado.update({"instalado": "antes", "commits": {"monitor": "viejo"}, "monitor_admin": "x"})
            lb.guardar()

            def revienta(*a, **kw):
                (lb.monitor).mkdir(exist_ok=True)
                (lb.monitor / "A_MEDIAS").write_text("x", "utf-8")
                raise OSError("sin internet (simulado)")
            lb._instalar = revienta
            with self.assertRaises(OSError):
                lb.instalar(True, False, False, {})
            self.assertTrue((lb.monitor / "VIEJO").exists())
            self.assertFalse((lb.monitor / "A_MEDIAS").exists())
            self.assertEqual(lab.Lab(lb.d).estado.get("commits"), {"monitor": "viejo"})
            self.assertFalse((lb.d / ".anterior").exists())

    def test_previo_detecta_puerto_ajeno_y_no_toca_nada(self):
        import socket
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            s = socket.socket()
            s.bind(("127.0.0.1", lb.p["network_engine"]))
            s.listen(1)
            try:
                r = lb.previo()
            finally:
                s.close()
            puertos = next(x for x in r["detalle"] if x["nombre"] == "puertos del laboratorio")
            self.assertFalse(r["ok"])
            self.assertTrue(puertos["bloqueante"])
            self.assertIn(str(lb.p["network_engine"]), puertos["detalle"])

    def test_previo_sin_problemas(self):
        with tempfile.TemporaryDirectory() as d:
            r = lab.Lab(Path(d) / "l").previo()
            self.assertTrue(r["reinstalar"])
            py = next(x for x in r["detalle"] if x["nombre"] == "python")
            # El laboratorio exige 3.11+ porque Monitor solo se prueba en 3.11–3.13. En la CI con 3.10 ese punto
            # debe bloquear (es lo correcto) y el resto debe estar bien.
            if sys.version_info >= (3, 11):
                self.assertTrue(r["ok"], r)
            else:
                self.assertFalse(r["ok"])
                self.assertTrue(py["bloqueante"] and not py["ok"])
                self.assertTrue(all(x["ok"] for x in r["detalle"] if x["nombre"] != "python"), r)

    def test_cancelacion_deja_informe_parcial_y_nada_aprobado(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            it = I.Integral(lb, "rapido", False, "github.com:443", None)
            def corta():
                raise KeyboardInterrupt
            it.p01_versiones = corta
            it.p15_cierre = lambda: {"estado": I.PASA, "detalle": "cerrado"}
            self.assertEqual(it.ejecutar(), 130)
            estados = {p["id"]: p["estado"] for p in it.pasos}
            self.assertEqual(estados["01"], I.CANCELADA)
            self.assertEqual(estados["15"], I.PASA)
            self.assertNotIn(I.PASA, [v for k, v in estados.items() if k != "15"])
            self.assertTrue((it.dir / "INFORME_VALIDACION_INTEGRAL.md").exists())


class SesionSupervisionTest(unittest.TestCase):
    """E5: una ejecución cortada dejaba viva la credencial «lab-supervision»; la siguiente no podía crear la suya (409)
    y la supervisión seguía SIN sesión y sin avisarlo. Ahora se revoca la sobrante y, si aun así falla, es un problema."""

    def _integral(self, d, tokens, falla_post=False):
        from types import SimpleNamespace
        from unittest import mock
        ne_dir = Path(d) / "ne"
        (ne_dir / "cauce").mkdir(parents=True)
        (ne_dir / "cauce" / "sessions.py").write_text("", "utf-8")
        it = object.__new__(I.Integral)
        it.lab = SimpleNamespace(ne=ne_dir, p={"network_engine": 1})

        def ne(ruta, metodo="GET", cuerpo=None):
            if ruta == "/tokens" and metodo == "GET":
                return 200, {"tokens": [dict(t) for t in tokens.values()]}
            if ruta == "/tokens" and metodo == "POST":
                if falla_post or any(t["name"] == cuerpo["name"] and not t["revoked_at"] for t in tokens.values()):
                    return 409, {"error": "name_in_use"}
                tid = f"{len(tokens):08x}"
                tokens[tid] = {"id": tid, "name": cuerpo["name"], "revoked_at": None}
                return 201, {"id": tid, "token": "x", "name": cuerpo["name"]}
            if ruta.startswith("/tokens/") and metodo == "DELETE":
                tokens[ruta.split("/")[-1]]["revoked_at"] = 1.0
                return 200, {}
            return 404, None
        it.ne = ne
        return it, mock.patch.object(I.L, "http", return_value=(201, {"id": "bs_1"}))

    def test_credencial_sobrante_de_una_ejecucion_cortada_no_impide_la_sesion(self):
        tokens = {"aaaaaaaa": {"id": "aaaaaaaa", "name": "lab-supervision", "revoked_at": None}}
        with tempfile.TemporaryDirectory() as d:
            it, parche = self._integral(d, tokens)
            with parche:
                s = it._sesion_supervision()
        self.assertIsNotNone(s)
        self.assertIsNone(it.sesion_motivo)
        self.assertEqual(tokens["aaaaaaaa"]["revoked_at"], 1.0)

    def test_si_no_se_puede_crear_queda_el_motivo(self):
        with tempfile.TemporaryDirectory() as d:
            it, parche = self._integral(d, {}, falla_post=True)
            with parche:
                s = it._sesion_supervision()
        self.assertIsNone(s)
        self.assertIn("409", it.sesion_motivo)


if __name__ == "__main__":
    unittest.main()
