"""Pruebas de regresión del instalador de Windows (solo se ejecutan en Windows).

Defecto real (PC MSI, Windows 11, Python 3.13): sin el lanzador ``py``,
install.ps1 abortaba con «No se encontró Python 3.10+» aunque ``python``
funcionaba. Se usa Windows PowerShell 5.1 (powershell.exe), que es lo que
invoca install.cmd.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "scripts", "windows", "install.ps1")


@unittest.skipUnless(sys.platform == "win32", "el instalador solo existe en Windows")
class WindowsInstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cauce-inst-")
        self.root = os.path.join(self.tmp, "instalacion")
        self.env = dict(os.environ)
        # 'python' debe resolverse al intérprete de esta prueba (p. ej. 3.13 en la CI)
        self.env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + self.env.get("PATH", "")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, *args):
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", INSTALL, "-InstallRoot", self.root, "-SkipDoctor", *args]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=self.env,
                           encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr

    def venv_version(self):
        vpy = os.path.join(self.root, "venv", "Scripts", "python.exe")
        self.assertTrue(os.path.exists(vpy), "no se creó el entorno virtual")
        out = subprocess.run([vpy, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return out

    def test_without_py_launcher_uses_python(self):
        code, out = self.run_install("-PythonCandidates", "py-no-existe,python")
        self.assertEqual(code, 0, out)
        self.assertIn("mediante 'python'", out)
        self.assertEqual(self.venv_version(), "%d.%d" % sys.version_info[:2])
        cauce = os.path.join(self.root, "bin", "cauce.cmd")
        p = subprocess.run(["cmd.exe", "/c", cauce, "verify-audit", os.path.join(self.tmp, "vacio.jsonl")],
                           capture_output=True, text=True, timeout=60, env=self.env)
        # el archivo no existe: lo importante es que el lanzador arranca Python del venv
        self.assertIn("vacio.jsonl", p.stdout + p.stderr)

    def test_broken_py_launcher_falls_back_to_python(self):
        fake = os.path.join(self.tmp, "py.cmd")
        with open(fake, "w") as fh:
            fh.write("@echo off\r\necho lanzador roto 1>&2\r\nexit /b 1\r\n")
        code, out = self.run_install("-PythonCandidates", f"{fake},python")
        self.assertEqual(code, 0, out)
        self.assertIn("mediante 'python'", out)

    def test_no_valid_python_fails_with_clear_message(self):
        code, out = self.run_install("-PythonCandidates", "py-no-existe,python-no-existe")
        self.assertNotEqual(code, 0)
        self.assertIn("No se encontró Python 3.10 o superior", out)
        self.assertIn("py-no-existe (no existe)", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, "venv")))

    def test_explicit_python_path(self):
        code, out = self.run_install("-Python", sys.executable)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.venv_version(), "%d.%d" % sys.version_info[:2])


if __name__ == "__main__":
    unittest.main()
