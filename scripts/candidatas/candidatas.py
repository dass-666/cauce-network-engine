"""E8b · Dos candidatas de laboratorio en el mismo equipo, SIN mezclarlas: RC2 (la de la prueba física) y RC3.

- RC2 NO se modifica: se usa tal cual desde su clon (por omisión %USERPROFILE%\\Cauce-AI-Center\\cauce-lab-e3\\ne-src, en
  6e2cceb) y su laboratorio (%USERPROFILE%\\cauce-lab-integrado). De RC2 solo se LEE (commit, estado, informes) y, si la
  persona lo pide, se lanza SU PROPIO 7_VALIDACION_INTEGRAL.cmd.
- RC3 vive aparte: este clon (por omisión %USERPROFILE%\\Cauce-AI-Center\\cauce-lab-rc3\\ne-src) y su laboratorio
  %USERPROFILE%\\cauce-lab-rc3, con los puertos desplazados +100 (Monitor 8200, Forms 8965, Network Engine 8980,
  servidor 8970). No toca C:\\cauce-msi, %LOCALAPPDATA%\\CauceNetworkEngine, respaldos, Forms de Ronaldo ni rutas globales.
- Nunca corren a la vez: cada orden comprueba que la otra candidata esté parada.

Órdenes: comprobar | instalar | validar {rc2,rc3} [perfil] | detener rc3 | comparar | huella-rc2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent.parent                       # este clon de Network Engine (RC3)
LAB_PKG = RAIZ / "scripts" / "laboratorio_integrado"

RC2 = {"network_engine": "6e2cceb02117cbea644351f7d7ee6f179a203ab7",
       "monitor": "9673702165831dfcd060a938a91153b1658dea53",
       "forms": "aa1bb0295b32c17ba0435490678adf1ee54c55a0"}
RC3 = {"monitor": "7b92446e884239b52c1253d57ee97d244fe4067b",
       "forms": "aa1bb0295b32c17ba0435490678adf1ee54c55a0"}
DESPLAZAMIENTO_RC3 = 100
PUERTOS_RC2 = (8100, 8865, 8880, 8870)
PUERTOS_RC3 = tuple(p + DESPLAZAMIENTO_RC3 for p in PUERTOS_RC2)


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def rc2_ne() -> Path:
    return Path(os.environ.get("CAUCE_RC2_NE") or home() / "Cauce-AI-Center" / "cauce-lab-e3" / "ne-src")


def rc2_lab() -> Path:
    return Path(os.environ.get("CAUCE_RC2_LAB") or home() / "cauce-lab-integrado")


def rc3_lab() -> Path:
    return Path(os.environ.get("CAUCE_RC3_LAB") or home() / "cauce-lab-rc3")


def decir(m: str) -> None:
    print(m, flush=True)


def git(repo: Path, *a) -> str:
    p = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def _dentro(a: Path, b: Path) -> bool:
    try:
        a.resolve().relative_to(b.resolve())
        return True
    except (ValueError, OSError):
        return False


def puerto_ocupado(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", p)) == 0


def pid_vivo(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def en_uso(lab: Path) -> dict | None:
    try:
        o = json.loads((lab / ".en_uso").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return o if pid_vivo(int(o.get("pid") or 0)) else None


def huella_rc2() -> dict:
    """Solo LECTURA de RC2: commit del clon, cambios sin guardar, y huellas de su estado.json y versiones.json."""
    ne, lab = rc2_ne(), rc2_lab()
    out = {"clon": str(ne), "laboratorio": str(lab), "existe_clon": ne.exists(), "existe_laboratorio": lab.exists()}
    if ne.exists():
        out["commit"] = git(ne, "rev-parse", "HEAD")
        out["cambios_sin_guardar"] = git(ne, "status", "--porcelain", "--untracked-files=no")
    for nombre, f in (("estado.json", lab / "estado.json"),
                      ("versiones.json", ne / "scripts" / "laboratorio_integrado" / "versiones.json")):
        try:
            out[nombre] = hashlib.sha256(Path(f).read_bytes()).hexdigest()
        except OSError:
            out[nombre] = None
    try:
        est = json.loads((lab / "estado.json").read_text("utf-8"))
        out["commits_instalados"] = {k: (est.get("commits") or {}).get(k) for k in ("network_engine", "monitor", "forms")}
    except (OSError, ValueError):
        out["commits_instalados"] = None
    return out


def comprobar(esperado: str | None = None) -> list[str]:
    """Errores que impiden usar RC3 aquí. Vacía = se puede seguir."""
    err = []
    head = git(RAIZ, "rev-parse", "HEAD")
    if not head:
        err.append(f"{RAIZ} no es un clon de git de Network Engine")
    if esperado and not head.startswith(esperado.lower()):
        err.append(f"este clon está en {head[:12]} y se esperaba {esperado}: ejecuta  git checkout --detach {esperado}")
    if git(RAIZ, "status", "--porcelain", "--untracked-files=no"):
        err.append("este clon tiene cambios sin guardar: RC3 debe instalarse desde un commit exacto, sin cambios")
    if _dentro(RAIZ, rc2_ne()) or _dentro(rc2_ne(), RAIZ):
        err.append(f"este clon es (o contiene) el clon de RC2 ({rc2_ne()}): RC3 necesita una carpeta propia")
    for prohibida in (Path("C:/cauce-msi"), Path(os.environ.get("LOCALAPPDATA") or "/nonexistent") / "CauceNetworkEngine",
                      rc2_lab()):
        if _dentro(RAIZ, prohibida) or _dentro(rc3_lab(), prohibida) or _dentro(prohibida, rc3_lab()):
            err.append(f"RC3 no puede vivir dentro de {prohibida} (ni al revés)")
    try:
        v = json.loads((LAB_PKG / "versiones.json").read_text("utf-8"))
        for comp in ("monitor", "forms"):
            if v[comp]["commit"] != RC3[comp]:
                err.append(f"versiones.json fija {comp} {v[comp]['commit'][:7]} y RC3 es {RC3[comp][:7]}")
    except (OSError, ValueError, KeyError) as e:
        err.append(f"no se pudo leer versiones.json: {e}")
    return err


def otra_parada(candidata: str) -> list[str]:
    otra_lab, puertos = (rc2_lab(), PUERTOS_RC2) if candidata == "rc3" else (rc3_lab(), PUERTOS_RC3)
    err = []
    o = en_uso(otra_lab)
    if o:
        err.append(f"la otra candidata está en uso ({otra_lab}: {o.get('quien')}, PID {o.get('pid')}). Espera a que "
                   "termine o ciérrala con Ctrl+C en su ventana.")
    ocupados = [p for p in puertos if puerto_ocupado(p)]
    if ocupados:
        err.append(f"puertos de la otra candidata en uso {ocupados}: detenla antes "
                   f"({'4_DETENER.cmd de RC2' if candidata == 'rc3' else 'DETENER_RC3.cmd'}).")
    return err


def entorno_rc3() -> dict:
    env = dict(os.environ)
    env["CAUCE_LAB_DESPLAZAMIENTO_PUERTOS"] = str(DESPLAZAMIENTO_RC3)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def lab_py(*args, env=None) -> int:
    return subprocess.call([sys.executable, str(LAB_PKG / "lab.py"), "--dir", str(rc3_lab()), *args],
                           cwd=str(LAB_PKG), env=env or entorno_rc3())


def instalar(esperado: str | None) -> int:
    antes = huella_rc2()
    err = comprobar(esperado) + otra_parada("rc3")
    if err:
        for e in err:
            decir("ERROR: " + e)
        return 2
    decir(f"RC3: Network Engine {git(RAIZ, 'rev-parse', 'HEAD')[:12]} · Monitor {RC3['monitor'][:7]} · "
          f"Forms {RC3['forms'][:7]}\nLaboratorio RC3: {rc3_lab()} (puertos {PUERTOS_RC3})")
    if lab_py("previo") != 0:
        decir("ERROR: la comprobación previa del laboratorio RC3 falló (ver arriba)")
        return 2
    rc = 0 if lab_py("al-dia") == 0 else lab_py("instalar", "--navegador-forms")
    despues = huella_rc2()
    intacta = antes == despues
    reg = {"cuando": time.strftime("%Y-%m-%d %H:%M:%S"), "network_engine": git(RAIZ, "rev-parse", "HEAD"),
           "monitor": RC3["monitor"], "forms": RC3["forms"], "laboratorio": str(rc3_lab()), "codigo_instalacion": rc,
           "rc2_antes": antes, "rc2_despues": despues, "rc2_intacta": intacta}
    rc3_lab().mkdir(parents=True, exist_ok=True)
    (rc3_lab() / "instalacion_rc3.json").write_text(json.dumps(reg, indent=1, ensure_ascii=False), "utf-8")
    decir(f"RC2 intacta (clon y laboratorio sin cambios): {'SÍ' if intacta else 'NO — revisa instalacion_rc3.json'}")
    decir("Instalación RC3: " + ("OK" if rc == 0 else "FALLÓ (si había una anterior, se restauró)"))
    return 0 if rc == 0 and intacta else 1


def validar(candidata: str, perfil: str, extra: list[str]) -> int:
    err = otra_parada(candidata)
    if candidata == "rc3":
        err = comprobar() + err
    else:
        h = huella_rc2()
        if h.get("commit") != RC2["network_engine"]:
            err.append(f"el clon de RC2 ({rc2_ne()}) no está en 6e2cceb ({(h.get('commit') or 'no existe')[:12]}): no se "
                       "toca; revisa la carpeta")
    if err:
        for e in err:
            decir("ERROR: " + e)
        return 2
    if candidata == "rc3":
        if lab_py("previo") != 0:
            return 2
        if lab_py("al-dia") != 0:
            decir("ERROR: el laboratorio RC3 no está instalado o no corresponde a este commit: ejecuta INSTALAR_RC3.cmd")
            return 2
        return subprocess.call([sys.executable, str(LAB_PKG / "integral.py"), "--dir", str(rc3_lab()), "--perfil", perfil,
                                *extra], cwd=str(LAB_PKG), env=entorno_rc3())
    # RC2: su PROPIO lanzador, tal cual, desde su clon (no se copia ni se cambia nada de RC2)
    cmd = rc2_ne() / "scripts" / "laboratorio_integrado" / "7_VALIDACION_INTEGRAL.cmd"
    env = {k: v for k, v in os.environ.items() if k != "CAUCE_LAB_DESPLAZAMIENTO_PUERTOS"}
    if os.name == "nt":
        return subprocess.call(["cmd", "/c", str(cmd), perfil, *extra], cwd=str(cmd.parent), env=env)
    return subprocess.call([sys.executable, "integral.py", "--perfil", perfil, *extra], cwd=str(cmd.parent), env=env)


def ultimo_informe(lab: Path) -> Path | None:
    xs = sorted((lab / "informes").glob("integral-*/validacion_integral.json"))
    return xs[-1] if xs else None


def comparar(salida: Path | None) -> int:
    filas, cab = {}, {}
    for cand, lab in (("RC2", rc2_lab()), ("RC3", rc3_lab())):
        f = ultimo_informe(lab)
        if not f:
            decir(f"ERROR: no hay informe de {cand} en {lab}\\informes")
            return 2
        d = json.loads(f.read_text("utf-8"))
        vers = {v["componente"]: (v.get("commit") or "")[:7] for p in d["pasos"] for v in (p.get("versiones") or [])}
        cab[cand] = {"informe": str(f.parent), "perfil": d.get("perfil"), "versiones": vers,
                     "resumen": {e: sum(1 for p in d["pasos"] if p["estado"] == e) for e in
                                 ("PASA", "FALLA", "PENDIENTE", "OMITIDA")},
                     "matriz": d.get("matriz")}
        for p in d["pasos"]:
            filas.setdefault(p["id"], {"titulo": p["titulo"]})[cand] = (p["estado"], p.get("evidencia"), p.get("s"))
    if cab["RC2"]["perfil"] != cab["RC3"]["perfil"]:
        decir(f"AVISO: perfiles distintos ({cab['RC2']['perfil']} frente a {cab['RC3']['perfil']}): la comparación no vale")
    lin = ["# Comparación RC2 frente a RC3 (mismo conjunto de pruebas)", ""]
    for c in ("RC2", "RC3"):
        lin.append(f"- **{c}**: perfil {cab[c]['perfil']} · versiones {cab[c]['versiones']} · {cab[c]['resumen']} · "
                   f"`{cab[c]['informe']}`")
    lin += ["", "| Paso | Prueba | RC2 | RC3 | ¿Igual? |", "|---|---|---|---|---|"]
    distintos = 0
    for pid in sorted(filas):
        r2, r3 = filas[pid].get("RC2"), filas[pid].get("RC3")
        igual = bool(r2 and r3 and r2[0] == r3[0])
        distintos += not igual
        fmt = (lambda r: f"{r[0]} ({r[1]}, {r[2]} s)" if r else "—")
        lin.append(f"| {pid} | {filas[pid]['titulo']} | {fmt(r2)} | {fmt(r3)} | {'sí' if igual else '**NO**'} |")
    lin += ["", f"Pasos con resultado distinto: **{distintos}**. Lo PENDIENTE no cuenta como aprobado. Lo simulado "
            "(evidencia SIMULADA/LOCAL) no es una prueba física."]
    texto = "\n".join(lin) + "\n"
    salida = salida or (rc3_lab() / "informes" / f"COMPARACION_RC2_RC3_{time.strftime('%Y%m%d-%H%M%S')}.md")
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(texto, "utf-8")
    (salida.with_suffix(".json")).write_text(json.dumps({"cabecera": cab, "pasos": filas}, indent=1, ensure_ascii=False),
                                             "utf-8")
    decir(texto)
    decir(f"Guardado en {salida}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("comprobar")
    c.add_argument("--esperado")
    i = sub.add_parser("instalar")
    i.add_argument("--esperado", help="commit de RC3 que se espera en este clon (se niega si no coincide)")
    v = sub.add_parser("validar")
    v.add_argument("candidata", choices=["rc2", "rc3"])
    v.add_argument("perfil", nargs="?", default="completo", choices=["rapido", "completo", "prolongado", "nocturno"])
    v.add_argument("extra", nargs=argparse.REMAINDER)
    d = sub.add_parser("detener")
    d.add_argument("candidata", choices=["rc3"])
    d.add_argument("--forzar", action="store_true")
    k = sub.add_parser("comparar")
    k.add_argument("--salida")
    sub.add_parser("huella-rc2")
    a = ap.parse_args(argv)
    if a.cmd == "comprobar":
        err = comprobar(a.esperado)
        decir(json.dumps({"rc3_clon": str(RAIZ), "rc3_commit": git(RAIZ, "rev-parse", "HEAD"), "rc3_laboratorio":
                          str(rc3_lab()), "errores": err, "rc2": huella_rc2()}, indent=1, ensure_ascii=False))
        return 1 if err else 0
    if a.cmd == "instalar":
        return instalar(a.esperado)
    if a.cmd == "validar":
        return validar(a.candidata, a.perfil, a.extra)
    if a.cmd == "detener":
        return lab_py("detener", *(["--forzar"] if a.forzar else []))
    if a.cmd == "comparar":
        return comparar(Path(a.salida) if a.salida else None)
    if a.cmd == "huella-rc2":
        decir(json.dumps(huella_rc2(), indent=1, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
