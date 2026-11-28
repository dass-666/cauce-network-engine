"""E10 · Combinación integral de LABORATORIO con Forms nativo, instalada APARTE de RC2 y RC3.

- Carpeta propia: %USERPROFILE%\\cauce-lab-e10 (CAUCE_E10_LAB para otra). Clon recomendado de Network Engine:
  %USERPROFILE%\\Cauce-AI-Center\\cauce-lab-e10\\ne-src, en el commit exacto que diga el informe E10.
- Puertos +200: Monitor 8300, Forms 9065, Network Engine 9080, servidor de laboratorio 9070 (RC2 usa +0, RC3 +100).
- Combinación: scripts/e10/versiones_e10.json (Monitor 14bfaac = RC3 + S5, Forms 933fe89 = 0.2.0rc2 candidata consolidada). El
  versiones.json del laboratorio (RC3) no se toca. Credenciales SOLO de laboratorio, creadas con la orden oficial:
  Monitor read y review, y una «session» para el puesto Forms (forms-lab-puesto). Nunca admin fuera del motor.
- No toca RC2 ni RC3 (se comprueba su huella antes y después), C:\\cauce-msi, %LOCALAPPDATA%\\CauceNetworkEngine,
  respaldos, Forms de Ronaldo ni rutas globales. No abre puertos públicos (todo en 127.0.0.1).
- Nunca a la vez que RC2 o RC3: cada orden comprueba que estén paradas.
- Restauración segura: «respaldar» y «restaurar» con la combinación PARADA. Restaurar:
  * nunca retrocede el registro de operaciones de Forms (perdería constancia de envíos posteriores al respaldo);
  * deja el estado de sesiones del motor como «copia anterior», así el motor arranca con una marca general y TODAS
    las sesiones pidiendo revisión humana (lo que pasó después del respaldo es desconocido);
  * aparta lo actual (datos.antes-de-restaurar-<fecha>), nunca lo borra.

Órdenes: comprobar | instalar --esperado COMMIT | validar [perfil] | jornadas [N...] | detener [--forzar] |
         respaldar | restaurar ZIP | huellas
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent.parent
LAB_PKG = RAIZ / "scripts" / "laboratorio_integrado"
sys.path.insert(0, str(RAIZ / "scripts" / "candidatas"))
import candidatas as C  # noqa: E402  (solo funciones de lectura: huellas, puertos, procesos)

VERSIONES = AQUI / "versiones_e10.json"
DESPLAZAMIENTO = 200
PUERTOS = tuple(p + DESPLAZAMIENTO for p in C.PUERTOS_RC2)
WINDOWS = os.name == "nt"


def e10_lab() -> Path:
    return Path(os.environ.get("CAUCE_E10_LAB") or C.home() / "cauce-lab-e10")


def respaldos() -> Path:
    return Path(os.environ.get("CAUCE_E10_RESPALDOS") or C.home() / "cauce-lab-e10-respaldos")


def rc3_ne() -> Path:
    return Path(os.environ.get("CAUCE_RC3_NE") or C.home() / "Cauce-AI-Center" / "cauce-lab-rc3" / "ne-src")


def decir(m: str) -> None:
    """Nunca se cae por la consola: en Windows sin UTF-8 (cp1252) un carácter que no existe se sustituye."""
    try:
        print(m, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(m.encode(enc, "replace").decode(enc, "replace"), flush=True)


def _sha(f: Path):
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()
    except OSError:
        return None


def huella(ne: Path, lab: Path) -> dict:
    """Solo LECTURA: commit y cambios del clon, huellas de estado.json y versiones.json, commits instalados."""
    out = {"clon": str(ne), "laboratorio": str(lab), "existe_clon": ne.exists(), "existe_laboratorio": lab.exists()}
    if ne.exists():
        out["commit"] = C.git(ne, "rev-parse", "HEAD")
        out["cambios_sin_guardar"] = C.git(ne, "status", "--porcelain", "--untracked-files=no")
    out["estado.json"] = _sha(lab / "estado.json")
    out["versiones.json"] = _sha(ne / "scripts" / "laboratorio_integrado" / "versiones.json")
    try:
        est = json.loads((lab / "estado.json").read_text("utf-8"))
        out["commits_instalados"] = est.get("commits")
    except (OSError, ValueError):
        out["commits_instalados"] = None
    return out


def huellas() -> dict:
    return {"rc2": huella(C.rc2_ne(), C.rc2_lab()), "rc3": huella(rc3_ne(), C.rc3_lab())}


def comprobar(esperado: str | None = None) -> list[str]:
    err = []
    head = C.git(RAIZ, "rev-parse", "HEAD")
    if not head:
        err.append(f"{RAIZ} no es un clon de git de Network Engine")
    if esperado and not head.startswith(esperado.lower()):
        err.append(f"este clon está en {head[:12]} y se esperaba {esperado}: ejecuta  git checkout --detach {esperado}")
    if C.git(RAIZ, "status", "--porcelain", "--untracked-files=no"):
        err.append("este clon tiene cambios sin guardar: E10 se instala desde un commit exacto, sin cambios")
    for nombre, otro in (("RC2", C.rc2_ne()), ("RC3", rc3_ne())):
        if C._dentro(RAIZ, otro) or C._dentro(otro, RAIZ):
            err.append(f"este clon es (o contiene) el clon de {nombre} ({otro}): E10 necesita una carpeta propia")
    for prohibida in (Path("C:/cauce-msi"), Path(os.environ.get("LOCALAPPDATA") or "/nonexistent") / "CauceNetworkEngine",
                      C.rc2_lab(), C.rc3_lab()):
        if C._dentro(RAIZ, prohibida) or C._dentro(e10_lab(), prohibida) or C._dentro(prohibida, e10_lab()):
            err.append(f"E10 no puede vivir dentro de {prohibida} (ni al revés)")
    try:
        v = json.loads(VERSIONES.read_text("utf-8"))
        for comp in ("monitor", "forms"):
            if len(v[comp]["commit"]) != 40:
                err.append(f"versiones_e10.json: el commit de {comp} no es exacto")
    except (OSError, ValueError, KeyError) as e:
        err.append(f"no se pudo leer versiones_e10.json: {e}")
    return err


def otras_paradas() -> list[str]:
    err = []
    for nombre, lab, puertos, como in (("RC2", C.rc2_lab(), C.PUERTOS_RC2, "4_DETENER.cmd de RC2"),
                                       ("RC3", C.rc3_lab(), C.PUERTOS_RC3, "DETENER_RC3.cmd")):
        o = C.en_uso(lab)
        if o:
            err.append(f"{nombre} está en uso ({lab}: {o.get('quien')}, PID {o.get('pid')}): espera a que termine")
        ocupados = [p for p in puertos if C.puerto_ocupado(p)]
        if ocupados:
            err.append(f"puertos de {nombre} en uso {ocupados}: detenla antes ({como})")
    return err


def parada() -> bool:
    return not C.en_uso(e10_lab()) and not any(C.puerto_ocupado(p) for p in PUERTOS)


def entorno() -> dict:
    env = dict(os.environ)
    env.update({"CAUCE_LAB_DESPLAZAMIENTO_PUERTOS": str(DESPLAZAMIENTO), "CAUCE_LAB_VERSIONES": str(VERSIONES),
                "CAUCE_LAB_09B_EXIGIR_NATIVO": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def lab_py(*args) -> int:
    return subprocess.call([sys.executable, str(LAB_PKG / "lab.py"), "--dir", str(e10_lab()), *args],
                           cwd=str(LAB_PKG), env=entorno())


def instalar(esperado: str | None, fuente_forms: str | None = None) -> int:
    antes = huellas()
    err = comprobar(esperado) + otras_paradas()
    if err:
        for e in err:
            decir("ERROR: " + e)
        return 2
    v = json.loads(VERSIONES.read_text("utf-8"))
    decir(f"E10: Network Engine {C.git(RAIZ, 'rev-parse', 'HEAD')[:12]} · Monitor {v['monitor']['commit'][:7]} · "
          f"Forms {v['forms']['commit'][:7]} ({v['forms']['version']})\nLaboratorio E10: {e10_lab()} (puertos {PUERTOS})")
    if (e10_lab() / "datos").exists():
        if not parada():
            decir("ERROR: la combinación E10 está en marcha: DETENER_E10.cmd antes de reinstalar")
            return 2
        decir(f"  Respaldo previo de los datos E10: {respaldar(silencioso=True)}")
    if lab_py("previo") != 0:
        decir("ERROR: la comprobación previa del laboratorio E10 falló (ver arriba)")
        return 2
    extra = ["--fuente-forms", fuente_forms] if fuente_forms else []
    rc = 0 if lab_py("al-dia") == 0 else lab_py("instalar", "--navegador-forms", *extra)
    despues = huellas()
    intactas = antes == despues
    reg = {"cuando": time.strftime("%Y-%m-%d %H:%M:%S"), "network_engine": C.git(RAIZ, "rev-parse", "HEAD"),
           "monitor": v["monitor"]["commit"], "forms": v["forms"]["commit"], "laboratorio": str(e10_lab()),
           "puertos": PUERTOS, "codigo_instalacion": rc, "rc2_rc3_antes": antes, "rc2_rc3_despues": despues,
           "rc2_rc3_intactas": intactas}
    e10_lab().mkdir(parents=True, exist_ok=True)
    (e10_lab() / "instalacion_e10.json").write_text(json.dumps(reg, indent=1, ensure_ascii=False), "utf-8")
    decir(f"RC2 y RC3 intactas (clones y laboratorios sin cambios): {'SÍ' if intactas else 'NO — revisa instalacion_e10.json'}")
    decir("Instalación E10: " + ("OK" if rc == 0 else "FALLÓ (si había una anterior, se restauró)"))
    return 0 if rc == 0 and intactas else 1


def validar(perfil: str, extra: list[str]) -> int:
    err = comprobar() + otras_paradas()
    if err:
        for e in err:
            decir("ERROR: " + e)
        return 2
    if lab_py("previo") != 0:
        return 2
    if lab_py("al-dia") != 0:
        decir("ERROR: el laboratorio E10 no está instalado o no corresponde a este commit: ejecuta INSTALAR_E10.cmd")
        return 2
    return subprocess.call([sys.executable, str(LAB_PKG / "integral.py"), "--dir", str(e10_lab()), "--perfil", perfil,
                            *extra], cwd=str(LAB_PKG), env=entorno())


def jornadas(ns: list[int], duracion_min: float, auditoria: bool) -> int:
    """Las jornadas arrancan SU propia copia de los tres motores en puertos libres al azar (127.0.0.1), con datos en
    informes/jornadas-<fecha>: no tocan los datos de la combinación instalada. Requieren la combinación parada."""
    err = otras_paradas() + ([] if parada() else ["la combinación E10 está en marcha: DETENER_E10.cmd antes"])
    if err:
        for e in err:
            decir("ERROR: " + e)
        return 2
    lab = e10_lab()
    bin_ = "Scripts" if WINDOWS else "bin"
    exe = "python.exe" if WINDOWS else "python"
    py_f, py_m = lab / "forms" / ".venv" / bin_ / exe, lab / "monitor" / ".venv" / bin_ / exe
    if not (py_f.exists() and py_m.exists()):
        decir("ERROR: falta la instalación E10 (INSTALAR_E10.cmd)")
        return 2
    subprocess.call([str(py_f), "-m", "pip", "install", "-q", "--disable-pip-version-check", "psutil"])   # solo medir
    salida = lab / "informes" / f"jornadas-{time.strftime('%Y%m%d-%H%M%S')}"
    # los navegadores que instaló INSTALAR_E10 (lab.py: datos/ms-playwright), no una carpeta nueva
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(lab / "datos" / NAVEGADORES), PYTHONIOENCODING="utf-8")
    args = [str(py_f), str(AQUI / "jornada_e10.py"), "--forms", str(lab / "forms"), "--monitor", str(lab / "monitor"),
            "--monitor-python", str(py_m), "--salida", str(salida)]
    args += ["--auditoria"] if auditoria else (["--duracion-min", str(duracion_min)] if duracion_min else
                                              ["--expedientes", *map(str, ns)])
    decir(f"Jornadas SIMULADAS E10 -> {salida}")
    return subprocess.call(args, cwd=str(AQUI), env=env)


NAVEGADORES = "ms-playwright"                    # como lab.py (Laboratorio.navegadores)


def respaldar(silencioso: bool = False) -> str:
    if not parada():
        raise SystemExit("ERROR: detén la combinación E10 antes de respaldar (DETENER_E10.cmd)")
    datos = e10_lab() / "datos"
    if not datos.exists():
        raise SystemExit(f"ERROR: no hay datos E10 en {datos}")
    respaldos().mkdir(parents=True, exist_ok=True)
    z = respaldos() / f"datos-e10-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(datos.rglob("*")):
            # el navegador (datos/ms-playwright) no es un dato: se reinstala y pesa cientos de MB
            if f.is_file() and f.relative_to(datos).parts[0] != NAVEGADORES:
                zf.write(f, f.relative_to(datos).as_posix())
    (z.with_suffix(".zip.sha256")).write_text(f"{_sha(z)}  {z.name}\n", "utf-8")
    if not silencioso:
        decir(f"Respaldo: {z} (+ .sha256). Contiene credenciales SOLO de laboratorio (cifradas con DPAPI en Windows).")
    return str(z)


def restaurar(zpath: str) -> int:
    z = Path(zpath)
    if not parada():
        decir("ERROR: detén la combinación E10 antes de restaurar (DETENER_E10.cmd)")
        return 2
    esperado = (z.with_suffix(".zip.sha256")).read_text("utf-8").split()[0] if z.with_suffix(".zip.sha256").exists() else None
    if not esperado or esperado != _sha(z):
        decir("ERROR: el respaldo no tiene huella o no coincide (.sha256): no se restaura nada")
        return 2
    datos = e10_lab() / "datos"
    aparte = e10_lab() / f"datos.antes-de-restaurar-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp = e10_lab() / "datos.restaurando"
    if tmp.exists():
        shutil.rmtree(tmp)
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():
            if n.startswith("/") or ".." in Path(n).parts:
                decir(f"ERROR: ruta no admitida en el respaldo: {n}")
                return 2
        zf.extractall(tmp)
    # 1) Forms: el registro de operaciones NUNCA retrocede (se conserva el actual, más completo)
    for rel in ("forms/estado/operaciones.json", "forms/estado/operaciones.json.bak", "forms/estado/huella.key",
                "forms/estado/puesto.json"):
        actual, nuevo = datos / rel, tmp / rel
        if actual.exists():
            nuevo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(actual, nuevo)
    # 2) Motor: el estado de sesiones restaurado se presenta como «copia anterior» → marca general al arrancar
    ses = tmp / "network-engine" / "sessions_state.json"
    if ses.exists():
        bak = ses.with_name("sessions_state.json.bak")
        if bak.exists():
            bak.unlink()
        ses.rename(bak)
    # 3) El navegador instalado se queda (no viaja en los respaldos)
    if (datos / NAVEGADORES).exists() and not (tmp / NAVEGADORES).exists():
        shutil.move(str(datos / NAVEGADORES), str(tmp / NAVEGADORES))
    if datos.exists():
        datos.rename(aparte)
    tmp.rename(datos)
    reg = {"cuando": time.strftime("%Y-%m-%d %H:%M:%S"), "respaldo": str(z), "sha256": esperado, "apartado": str(aparte),
           "forms_operaciones": "conservado el actual (no retrocede)",
           "network_engine_sesiones": "restaurado como copia anterior: marca general y revisión humana de todas"}
    (e10_lab() / f"restauracion-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(reg, indent=1, ensure_ascii=False), "utf-8")
    decir(json.dumps(reg, indent=1, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("comprobar")
    c.add_argument("--esperado")
    i = sub.add_parser("instalar")
    i.add_argument("--esperado", help="commit de Network Engine que se espera en este clon (se niega si no coincide)")
    i.add_argument("--fuente-forms", help="copiar Forms de esta carpeta (en su commit exacto) en vez de descargarlo")
    v = sub.add_parser("validar")
    v.add_argument("perfil", nargs="?", default="completo", choices=["rapido", "completo", "prolongado", "nocturno"])
    v.add_argument("extra", nargs=argparse.REMAINDER)
    j = sub.add_parser("jornadas")
    j.add_argument("expedientes", nargs="*", type=int, default=[6, 10, 20, 50])
    j.add_argument("--duracion-min", type=float, default=0)
    j.add_argument("--auditoria", action="store_true")
    d = sub.add_parser("detener")
    d.add_argument("--forzar", action="store_true")
    sub.add_parser("respaldar")
    r = sub.add_parser("restaurar")
    r.add_argument("zip")
    sub.add_parser("huellas")
    a = ap.parse_args(argv)
    if a.cmd == "comprobar":
        err = comprobar(a.esperado) + otras_paradas()
        decir(json.dumps({"e10_clon": str(RAIZ), "e10_commit": C.git(RAIZ, "rev-parse", "HEAD"), "e10_laboratorio":
                          str(e10_lab()), "puertos": PUERTOS, "errores": err, "otras": huellas()}, indent=1, ensure_ascii=False))
        return 1 if err else 0
    if a.cmd == "instalar":
        return instalar(a.esperado, a.fuente_forms)
    if a.cmd == "validar":
        return validar(a.perfil, a.extra)
    if a.cmd == "jornadas":
        return jornadas(a.expedientes, a.duracion_min, a.auditoria)
    if a.cmd == "detener":
        return lab_py("detener", *(["--forzar"] if a.forzar else []))
    if a.cmd == "respaldar":
        respaldar()
        return 0
    if a.cmd == "restaurar":
        return restaurar(a.zip)
    if a.cmd == "huellas":
        decir(json.dumps(huellas(), indent=1, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
