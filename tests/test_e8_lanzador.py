"""E8: robustez del lanzador del laboratorio (hallazgos L1, L2 y L3 de E7)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "scripts" / "laboratorio_integrado"))
import integral as I  # noqa: E402
import lab  # noqa: E402


def _otro_proceso_vivo():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


class DetenerConValidacionEnCursoTest(unittest.TestCase):
    """L1: 4_DETENER mataba sin aviso los motores de una validación en curso."""

    def test_detener_se_niega_si_otro_proceso_tiene_el_laboratorio(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            p = _otro_proceso_vivo()
            try:
                (lb.d / ".en_uso").write_text(json.dumps({"pid": p.pid, "quien": "integral completo", "desde": "x"}),
                                              "utf-8")
                lb.estado["pids"] = {"monitor": p.pid}
                with self.assertRaises(lab.Ocupado) as cm:
                    lb.detener()
                self.assertIn("Ctrl+C", str(cm.exception))
                self.assertIsNone(p.poll(), "no debe tocar nada")
                self.assertEqual(lb.estado["pids"], {"monitor": p.pid})
            finally:
                p.kill()
                p.wait()

    def test_forzar_lo_anota_y_la_validacion_no_cuenta(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            p = _otro_proceso_vivo()
            try:
                (lb.d / ".en_uso").write_text(json.dumps({"pid": p.pid, "quien": "integral", "desde": "x"}), "utf-8")
                lb.detener(forzar=True)
                linea = json.loads((lb.d / "informes" / "detenciones_forzadas.jsonl").read_text("utf-8").splitlines()[0])
                self.assertIn("NO cuenta", linea["aviso"])
            finally:
                p.kill()
                p.wait()

    def test_el_propio_dueno_del_bloqueo_si_puede_detener(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            with lab.Bloqueo(lb.d, "integral"):
                self.assertEqual(lb.detener(), {})

    def test_bloqueo_de_un_proceso_terminado_no_impide_detener(self):
        with tempfile.TemporaryDirectory() as d:
            lb = lab.Lab(Path(d) / "l")
            lab.validar_carpeta(lb.d, crear=True)
            (lb.d / ".en_uso").write_text('{"pid": 999999999, "quien": "cortada"}', "utf-8")
            self.assertEqual(lb.detener(), {})

    def test_el_lanzador_pasa_forzar(self):
        txt = (RAIZ / "scripts" / "laboratorio_integrado" / "4_DETENER.cmd").read_bytes()
        self.assertIn(b"lab.py detener %*", txt)
        self.assertIn(b"\r\n", txt)


@unittest.skipIf(lab.WINDOWS or not Path("/proc").is_dir(), "zombis: solo Linux")
class ZombiTest(unittest.TestCase):
    """L2: un proceso terminado sin recoger (zombi) se contaba como vivo."""

    def test_zombi_no_esta_vivo_ni_retiene_el_bloqueo(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        t0 = time.time()
        while time.time() - t0 < 10:            # se espera a que termine SIN recogerlo (queda zombi)
            try:
                if Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            except OSError:
                break
            time.sleep(0.05)
        try:
            self.assertFalse(lab.pid_vivo(p.pid))
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / ".en_uso").write_text(json.dumps({"pid": p.pid, "quien": "zombi"}), "utf-8")
                with lab.Bloqueo(d, "nueva"):
                    pass
        finally:
            p.wait()

    def test_muestreo_no_cuenta_un_zombi_como_vivo(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        time.sleep(1.0)
        try:
            with tempfile.TemporaryDirectory() as d:
                lab.validar_carpeta(Path(d) / "l", crear=True)
                it = I.Integral(lab.Lab(Path(d) / "l"), "rapido", False, None, preguntar=lambda _m: "")
                it.lab.estado["pids"] = {"monitor": p.pid}
                self.assertFalse(it._procesos()["monitor"]["vivo"])
        finally:
            p.wait()


class Intf:
    def __init__(self):
        self.usable, self.gateway, self.name, self.description = True, "192.168.42.129", "Ethernet 5", "Remote NDIS"


class TelefonoSegundoIntentoTest(unittest.TestCase):
    """L3: pulsar Enter antes de que Windows reconozca el teléfono daba FALLA tras un único intento."""

    def _it(self, d, respuestas):
        lab.validar_carpeta(Path(d) / "l", crear=True)
        resp = iter(respuestas)
        it = I.Integral(lab.Lab(Path(d) / "l"), "completo", True, None, preguntar=lambda _m: next(resp))
        it.ESPERA_TELEFONO_S = 0.2
        it.resumen_if = lambda c: {"nombre": c.name}
        return it

    def test_aparece_en_el_segundo_intento(self):
        with tempfile.TemporaryDirectory() as d:
            it = self._it(d, ["", ""])
            vistas = {"n": 0}

            def telefonos():
                vistas["n"] += 1
                return [Intf()] if vistas["n"] > 2 else []   # 1: comprobación inicial, 2: intento 1
            with mock.patch.object(it, "_telefonos", side_effect=telefonos), mock.patch.object(I.L, "WINDOWS", True):
                r = it.p06_telefono()
            self.assertEqual(r["estado"], I.PASA, r)
            self.assertGreaterEqual(len(r["intentos"]), 2)

    def test_omitir_en_el_segundo_intento_queda_pendiente_no_falla(self):
        with tempfile.TemporaryDirectory() as d:
            it = self._it(d, ["", "omitir"])
            with mock.patch.object(it, "_telefonos", return_value=[]):
                r = it.p06_telefono()
            self.assertEqual(r["estado"], I.PENDIENTE, r)
            self.assertTrue(it.intervenciones)

    def test_tres_intentos_sin_telefono_es_falla_con_detalle(self):
        with tempfile.TemporaryDirectory() as d:
            it = self._it(d, ["", "", ""])
            with mock.patch.object(it, "_telefonos", return_value=[]):
                r = it.p06_telefono()
            self.assertEqual((r["estado"], len(r["intentos"])), (I.FALLA, 3), r)


if __name__ == "__main__":
    unittest.main()
