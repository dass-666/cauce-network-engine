"""Paquete de laboratorio integrado (scripts/laboratorio_integrado): salvaguardas que no dependen de Windows."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts" / "laboratorio_integrado"))
import lab  # noqa: E402
from cauce import DEFAULT_API_PORT, __version__  # noqa: E402


class LabIntegradoTest(unittest.TestCase):
    def test_versiones_coinciden_con_este_network_engine(self):
        v = json.loads((RAIZ / "scripts" / "laboratorio_integrado" / "versiones.json").read_text("utf-8"))
        self.assertEqual(v["network_engine"]["version"], __version__)
        for k in ("monitor", "forms"):
            self.assertRegex(v[k]["commit"], r"^[0-9a-f]{40}$", "commit exacto, no una rama")

    def test_puertos_propios_no_chocan_con_las_instalaciones_normales(self):
        puertos = set(lab.VERSIONES["puertos"].values())
        self.assertEqual(len(puertos), 4)
        self.assertFalse(puertos & {8000, 8765, 8770, 8780, DEFAULT_API_PORT, 8200, 8201})

    def test_no_usa_instalaciones_existentes_ni_carpetas_ajenas(self):
        with tempfile.TemporaryDirectory() as d:
            ajena = Path(d) / "ajena"
            ajena.mkdir()
            (ajena / "algo.txt").write_text("x")
            with self.assertRaises(SystemExit):
                lab.validar_carpeta(ajena, crear=True)
            real = Path(d) / "real"
            real.mkdir()
            (real / ".env").write_text("MONITOREO_ENTORNO=produccion")
            with self.assertRaises(SystemExit):
                lab.validar_carpeta(real, crear=True)
            with mock.patch.object(lab, "prohibidas", return_value=[Path(d) / "cauce-msi"]):
                with self.assertRaises(SystemExit):
                    lab.validar_carpeta(Path(d) / "cauce-msi" / "lab", crear=True)
            nueva = Path(d) / "nueva"
            lab.validar_carpeta(nueva, crear=True)
            self.assertTrue((nueva / lab.MARCA).exists())
            lab.validar_carpeta(nueva, crear=False)      # la suya sí la reutiliza

    def test_entorno_limpio_quita_configuracion_heredada(self):
        with mock.patch.dict(os.environ, {"MONITOREO_DB_URL": "postgresql://real", "CAUCE_NE_PORT": "1",
                                          "PLAYWRIGHT_BROWSERS_PATH": "x", "OTRA": "y"}):
            env = lab.entorno_limpio({"MONITOREO_PUERTO": "8100"})
        self.assertNotIn("MONITOREO_DB_URL", env)
        self.assertNotIn("CAUCE_NE_PORT", env)
        self.assertNotIn("PLAYWRIGHT_BROWSERS_PATH", env)
        self.assertEqual((env["OTRA"], env["MONITOREO_PUERTO"]), ("y", "8100"))

    def test_modo_real_solo_sondea_tu_servidor_y_sin_rotacion(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            with self.assertRaises(SystemExit):
                l._cfg_ne("real", None)
            cfg = json.loads(l._cfg_ne("real", "http://mi-servidor.example:8770").read_text("utf-8"))
            self.assertEqual(cfg["health"]["targets"], ["mi-servidor.example:8770"])
            self.assertIs(cfg["lab"]["rotation_enabled"], False)
            self.assertEqual(cfg["api"]["port"], lab.VERSIONES["puertos"]["network_engine"])

    def test_detener_no_toca_procesos_ajenos(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            l.estado["pids"] = {"monitor": os.getpid()}            # un PID que NO es Monitor (es esta prueba)
            with mock.patch.object(l, "_cmdline", return_value="python -m unittest"), \
                    mock.patch("os.killpg", create=True) as kill, mock.patch("subprocess.run") as run:
                self.assertEqual(l.detener(), {"monitor": "ya no estaba en marcha"})
            kill.assert_not_called()
            run.assert_not_called()

    def test_f11_del_guion_usa_los_mismos_puertos_que_el_laboratorio(self):
        from cauce.fieldcheck import ENGINES_LAB
        p = lab.VERSIONES["puertos"]
        self.assertEqual([x[1] for x in ENGINES_LAB.values()], [p["monitor"], p["forms"], p["network_engine"]])

    def test_modo_real_exige_que_tu_servidor_responda_antes_de_arrancar(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            l = lab.Lab(Path(d) / "l")
            with mock.patch.object(lab, "http", return_value=(None, None)), mock.patch.object(l, "_lanzar") as lanzar:
                r = l.iniciar("real", "http://127.0.0.1:9", abrir=False)
            self.assertFalse(r["ok"])
            self.assertIn("servidor de laboratorio", r["motivo"])
            lanzar.assert_not_called()

    def test_forms_con_navegador_solo_en_localhost_y_navegador_dentro_del_laboratorio(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            lb = lab.Lab(Path(d) / "l")
            with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": "C:/global", "CAUCE_ALLOWED_HOSTS_FILE": "x"}):
                env = lb.entorno_forms()
            self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(lb.navegadores))
            self.assertTrue(str(lb.navegadores).startswith(str(lb.d)))
            hosts = json.loads(Path(env["CAUCE_ALLOWED_HOSTS_FILE"]).read_text("utf-8"))
            self.assertEqual(hosts, {"allowed_hosts": []}, "ningún dominio de terceros autorizado")
            self.assertEqual(env["CAUCE_HEADLESS"], "1")

    def test_forms_sin_chromium_se_omite_y_no_cuenta_como_aprobada(self):
        with tempfile.TemporaryDirectory() as d:
            lab.validar_carpeta(Path(d) / "l", crear=True)
            lb = lab.Lab(Path(d) / "l")
            lb.forms.mkdir()
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAUCE_LAB_BROWSER_PATH", None)
                r = lb.prueba_forms()
            self.assertFalse(r["ok"])
            self.assertTrue(r["omitida"])
            self.assertIn("--navegador-forms", r["motivo"])

    def test_lanzadores_ascii_crlf(self):
        for f in (RAIZ / "scripts" / "laboratorio_integrado").glob("*.cmd"):
            b = f.read_bytes()
            self.assertTrue(all(x < 128 for x in b), f.name)
            self.assertNotIn(b"\n", b.replace(b"\r\n", b""), f.name)
            self.assertIn(b"lab.py", b)


if __name__ == "__main__":
    unittest.main()
