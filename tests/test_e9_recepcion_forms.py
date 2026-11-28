"""E9: herramientas para RECIBIR la integración de Forms (no tocan Forms). Comprobador estático y verificador dinámico."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
RF = RAIZ / "scripts" / "recepcion_forms"
sys.path.insert(0, str(RF))
import comprobar_forms  # noqa: E402


def _repo(tmp_path: Path, archivos: dict) -> tuple[Path, str]:
    d = tmp_path / "forms"
    d.mkdir()
    g = ["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "LEEME.md").write_text("Forms ficticio\n", "utf-8")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "base"], check=True)
    base = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    for rel, contenido in archivos.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contenido, Path):
            shutil.copy(contenido, p)
        else:
            p.write_text(contenido, "utf-8")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "integracion"], check=True)
    return d, base


def _estados(out):
    return {x["id"]: x["estado"] for x in out["resultados"]}


def test_integracion_correcta_es_apta(tmp_path):
    d, base = _repo(tmp_path, {
        "cauce_forms/red/client.py": RAIZ / "cauce" / "client.py",
        "cauce_forms/red/session_consumer.py": RAIZ / "cauce" / "session_consumer.py",
        "cauce_forms/red/ORIGEN.md": "Copiado de Network Engine RC3 f93dd58eeddc1cc3032711bb0b6e378708063d41\n",
        "cauce_forms/sesion.py": "cli = CauceSessionClient(tok, port=p, timeout=5)\nsesion.register('forms-ventanilla-1')\n"
                                 "MENSAJE = 'sin información de red'\n",
    })
    out = comprobar_forms.comprobar(d, base)
    assert comprobar_forms.veredicto(out["resultados"]) == "APTO_PARA_PRUEBA_DINAMICA", out


def test_reglas_rotas_se_detectan(tmp_path):
    cliente_cambiado = (RAIZ / "cauce" / "client.py").read_text("utf-8") + "\n# retocado\n"
    d, base = _repo(tmp_path, {
        "cauce_forms/red/client.py": cliente_cambiado,
        "cauce_forms/sesion.py": "cli = CauceSessionClient(tok, port=p, timeout=2)\n"
                                 "sesion.register(f'forms-{uuid.uuid4().hex[:6]}')\nMENSAJE = 'Sin internet'\n"
                                 "subprocess.run(['cauce','token','create','--scope', 'admin'])\nc.resolve_pending(x, n, e)\n",
        "config/allowed_hosts.json": '{"allowed_hosts": ["127.0.0.1", "portal.ejemplo.org"]}',
    })
    e = _estados(comprobar_forms.comprobar(d, base))
    assert e["rc3.client.py"] == "NO_APTO"            # distinto de RC3
    assert e["rc3.session_consumer.py"] == "NO_APTO"  # falta
    assert e["latidos.timeout"] == "NO_APTO"
    assert e["etiqueta.estable"] == "REVISAR"
    assert e["interfaz.sin_internet"] == "NO_APTO"
    assert e["credencial.ambito"] == "NO_APTO"
    assert e["credencial.no_resuelve"] == "NO_APTO"
    assert e["portales.hosts"] == "REVISAR"


def test_cambios_sin_guardar_no_apto(tmp_path):
    d, base = _repo(tmp_path, {"x.py": "a = 1\n"})
    (d / "x.py").write_text("a = 2\n", "utf-8")
    assert _estados(comprobar_forms.comprobar(d, base))["git.limpio"] == "NO_APTO"


@pytest.mark.parametrize("modo,esperado", [("correcto", 0), ("repite", 1)])
def test_verificador_dinamico_distingue_si_forms_repite(tmp_path, modo, esperado):
    r = subprocess.run([sys.executable, str(RF / "verificar_sesiones_forms.py"), "--puerto", "8997",
                        "--etiqueta", "forms-ventanilla-1", "--forms", str(RAIZ), "--salida", str(tmp_path / "out"),
                        "--reinicio-en-curso", "--cortes", "1", "--",
                        sys.executable, str(RF / "forms_ficticio.py"), modo, "forms-ventanilla-1"],
                       capture_output=True, text=True, timeout=240)
    assert r.returncode == esperado, r.stdout + r.stderr
    if modo == "repite":
        assert "FALLA Forms NO volvió a poner la operación «en curso»" in r.stdout
