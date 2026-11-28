"""E9 · Comprobación ESTÁTICA de compatibilidad de una rama de Cauce Forms con Network Engine RC3 (solo lee).

No modifica Forms, no ejecuta su código y no sale a internet. Revisa el árbol de una copia local de la rama de Forms:
- que parta de aa1bb02 y esté limpia;
- que el cliente y el consumidor de sesiones sean EXACTAMENTE los de RC3 (SHA-256) y que el commit de RC3 esté anotado;
- reglas del contrato que se pueden ver en el texto: ámbito de la credencial, tiempo de espera del cliente, etiqueta
  estable, «sin internet», confirmado_recepcion, hosts permitidos y portales externos.

Las comprobaciones de texto son HEURÍSTICAS: un «OK» no demuestra el comportamiento; para eso está
verificar_sesiones_forms.py (dinámica) y el laboratorio integrado. Un «REVISAR» puede ser un falso positivo: se mira a mano.

Uso: python comprobar_forms.py RUTA_FORMS [--salida DIR]
Salida: 0 = APTO para la prueba dinámica, 1 = REVISAR, 2 = NO APTO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CONTRATO = json.loads((AQUI / "contrato_forms_rc3.json").read_text("utf-8"))
IGNORAR_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", "logs", "state", "evidence", "dist", "build",
                "ms-playwright", ".pytest_cache"}
CODIGO = {".py", ".js", ".ts", ".html", ".bat", ".cmd", ".ps1", ".json", ".toml", ".cfg", ".ini", ".yml", ".yaml"}
DOCS = {".md", ".txt", ".rst"}


def git(ruta: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(ruta), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout.strip()


def archivos(raiz: Path):
    for p in raiz.rglob("*"):
        if p.is_file() and not (set(p.relative_to(raiz).parts[:-1]) & IGNORAR_DIRS) and p.stat().st_size < 2_000_000:
            yield p


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def comprobar(raiz: Path, base: str | None = None) -> dict:
    res: list[dict] = []

    def r(id_, estado, detalle, donde=None):
        res.append({"id": id_, "estado": estado, "detalle": detalle, **({"donde": donde[:10]} if donde else {})})

    # 1 · Git
    rc, head = git(raiz, "rev-parse", "HEAD")
    if rc != 0:
        r("git", "NO_APTO", "no es un repositorio git")
        return {"resultados": res}
    _, rama = git(raiz, "rev-parse", "--abbrev-ref", "HEAD")
    _, sucio = git(raiz, "status", "--porcelain")
    base_rc, _ = git(raiz, "merge-base", "--is-ancestor", base or CONTRATO["candidata"]["forms_base"], "HEAD")
    r("git.commit", "INFO", f"{head} (rama {rama})")
    r("git.limpio", "OK" if not sucio else "NO_APTO", "sin cambios sin guardar" if not sucio else "hay cambios sin guardar")
    r("git.base_aa1bb02", "OK" if base_rc == 0 else "NO_APTO",
      "parte de aa1bb02" if base_rc == 0 else "no parte de aa1bb02 (o falta ese commit en la copia local)")

    textos: dict[Path, str] = {}
    for p in archivos(raiz):
        if p.suffix.lower() in CODIGO | DOCS:
            try:
                textos[p] = p.read_text("utf-8", errors="replace")
            except OSError:
                pass
    codigo = {p: t for p, t in textos.items() if p.suffix.lower() in CODIGO}

    def rel(p: Path) -> str:
        return str(p.relative_to(raiz)).replace("\\", "/")

    # 2 · Cliente y consumidor de RC3, idénticos
    for nombre, esperado in CONTRATO["archivos_de_referencia"].items():
        base = Path(nombre).name
        marca = "class CauceSessionClient" if base == "client.py" else "class SesionDiagnostico"
        copias = [p for p, t in codigo.items() if p.name == base and marca in t]
        if not copias:
            r(f"rc3.{base}", "NO_APTO", f"no hay copia de {nombre} de RC3 (con «{marca}»)")
            continue
        malas = [rel(p) for p in copias if sha(p) != esperado]
        r(f"rc3.{base}", "OK" if not malas else "NO_APTO",
          f"{len(copias)} copia(s) idéntica(s) a RC3" if not malas else "copia distinta de la de RC3 (SHA-256)", malas or
          [rel(p) for p in copias])
    anotado = [rel(p) for p, t in textos.items() if CONTRATO["candidata"]["network_engine"][:7] in t]
    r("rc3.commit_anotado", "OK" if anotado else "REVISAR",
      "el commit de RC3 está anotado" if anotado else "no aparece f93dd58 en el código ni en la documentación", anotado)

    # 3 · Reglas de texto (heurísticas)
    def buscar(patron, solo_codigo=True, flags=re.I):
        rx = re.compile(patron, flags)
        out = []
        for p, t in (codigo if solo_codigo else textos).items():
            if p.name in ("client.py", "session_consumer.py"):
                continue
            for n, linea in enumerate(t.splitlines(), 1):
                if rx.search(linea):
                    out.append(f"{rel(p)}:{n}: {linea.strip()[:120]}")
        return out

    prohib = buscar(r"""(--scope["']?[\s,=]*["']?|scope["']?\s*[:=]\s*["'])(admin|review)\b""")
    r("credencial.ambito", "NO_APTO" if prohib else "OK",
      "usa una credencial admin o review (Forms solo puede usar «session»)" if prohib else "no se ve ámbito admin/review", prohib)
    resolver = buscar(r"/resolve\b|resolve_pending|resolve_session")
    r("credencial.no_resuelve", "NO_APTO" if resolver else "OK",
      "Forms llama a la resolución de revisiones (prohibido)" if resolver else "no resuelve marcas", resolver)
    tiempos = []
    for linea in buscar(r"CauceSessionClient\(.*timeout\s*=\s*([0-9.]+)"):
        m = re.search(r"timeout\s*=\s*([0-9.]+)", linea)
        if m and float(m.group(1)) < CONTRATO["latidos"]["timeout_cliente_minimo_s"]:
            tiempos.append(linea)
    r("latidos.timeout", "NO_APTO" if tiempos else "OK",
      "tiempo de espera del cliente < 5 s" if tiempos else "no se ve un timeout < 5 s", tiempos)
    etiquetas = buscar(r"register\(\s*f?[\"'].*(\{.*(time|uuid|random|datetime|now|sid|id)\b.*\}|uuid|time\.time)")
    r("etiqueta.estable", "REVISAR" if etiquetas else "OK",
      "la etiqueta parece cambiar en cada sesión (debe ser estable por puesto)" if etiquetas else
      "no se ve una etiqueta variable", etiquetas)
    internet = buscar(r"sin\s+internet")
    r("interfaz.sin_internet", "NO_APTO" if internet else "OK",
      "aparece «sin internet» (debe decir «sin información de red»)" if internet else "no aparece «sin internet»", internet)
    conf = buscar(r"confirmado_recepcion\s*[:=]\s*True")
    r("interfaz.confirmado_recepcion", "REVISAR" if conf else "OK",
      "confirmado_recepcion = True literal: debe salir solo de un folio o acuse" if conf else "no hay True literal", conf)
    externos = buscar(r"fonacot", solo_codigo=True)
    r("portales.fonacot", "NO_APTO" if externos else "OK",
      "el código menciona FONACOT (no se usa ni se sondea)" if externos else "sin referencias en el código", externos)
    hosts = []
    for p, t in codigo.items():
        if "allowed_hosts" in p.name.lower() and p.suffix == ".json":
            try:
                h = json.loads(t).get("allowed_hosts", [])
            except (ValueError, AttributeError):
                continue
            hosts += [f"{rel(p)}: {x}" for x in h if x not in ("127.0.0.1", "localhost", "::1")]
    r("portales.hosts", "REVISAR" if hosts else "OK",
      "hay hosts permitidos fuera de 127.0.0.1 (solo portal propio de laboratorio)" if hosts else "solo locales", hosts)
    return {"forms": str(raiz), "commit": head, "resultados": res}


def veredicto(res: list[dict]) -> str:
    estados = {x["estado"] for x in res}
    return "NO_APTO" if "NO_APTO" in estados else "REVISAR" if "REVISAR" in estados else "APTO_PARA_PRUEBA_DINAMICA"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("forms")
    ap.add_argument("--salida")
    ap.add_argument("--base", help="solo para pruebas: commit base distinto de aa1bb02")
    a = ap.parse_args(argv)
    out = comprobar(Path(a.forms).resolve(), a.base)
    out["veredicto"] = veredicto(out["resultados"])
    out["contrato"] = CONTRATO["version_contrato"]
    out["nota"] = "Comprobación estática y heurística. No sustituye la prueba dinámica ni el laboratorio integrado."
    lineas = [f"# Compatibilidad Forms ↔ Network Engine RC3 (estática)", "",
              f"- Forms: `{out.get('commit')}`", f"- Veredicto: **{out['veredicto']}**", f"- {out['nota']}", "",
              "| Comprobación | Estado | Detalle |", "|---|---|---|"]
    for x in out["resultados"]:
        lineas.append(f"| {x['id']} | {x['estado']} | {x['detalle']}" + (f" ({'; '.join(x['donde'][:3])})" if x.get('donde') and x['estado'] != 'OK' else "") + " |")
    md = "\n".join(lineas) + "\n"
    if a.salida:
        d = Path(a.salida)
        d.mkdir(parents=True, exist_ok=True)
        (d / "compatibilidad_forms.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), "utf-8")
        (d / "compatibilidad_forms.md").write_text(md, "utf-8")
    sys.stdout.write(md)
    return {"APTO_PARA_PRUEBA_DINAMICA": 0, "REVISAR": 1}.get(out["veredicto"], 2)


if __name__ == "__main__":
    sys.exit(main())
