"""Portal PROPIO de laboratorio para E10: el portal ficticio de Forms (``lab_portal/servidor.py``, solo LECTURA de su
código) con dos añadidos del laboratorio, sin modificar el repositorio de Forms:

1. ``GET /_lab/e10/recibidos``: cada recepción con los campos FICTICIOS que llegaron (para comprobar desde fuera que
   ningún expediente se mezcló con otro: todos los campos de una recepción deben ser los de UNA misma fila).
2. ``POST /_lab/e10/modo {"sin_token": true}``: el portal deja de ser idempotente (cada POST cuenta como trámite nuevo,
   aunque repita token). Sirve para separar lo que protege el SOFTWARE de lo que protege el PORTAL: con un portal real
   sin token de un solo uso, un reintento propio de Chromium o un reenvío serían un duplicado.

Solo 127.0.0.1 y datos ficticios. Uso: python portal_e10.py --forms RUTA_FORMS --port N [--capacidad C --por-minuto M]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlencode


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forms", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--capacidad", type=int, default=100000)
    ap.add_argument("--por-minuto", type=int, default=1000)
    a = ap.parse_args()
    sys.path.insert(0, str(Path(a.forms).resolve()))
    from lab_portal import servidor as S

    registro: list[dict] = []
    modo = {"sin_token": False}
    lock = threading.Lock()
    H = S._Handler
    recibir0, get0, post0 = H._recibir, H.do_GET, H.do_POST

    def _recibir(self, raw: bytes) -> None:
        p = self.portal
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if modo["sin_token"]:
            form["cauce_token_formulario"] = p.emitir()          # portal SIN protección: todo POST es un trámite nuevo
            raw = urlencode(form).encode()
        antes = set(p.recibidos)
        try:
            recibir0(self, raw)
        finally:
            with p.lock:
                nuevos = [t for t in p.recibidos if t not in antes]
            with lock:
                for t in nuevos:
                    registro.append({"folio": p.recibidos[t]["folio"],
                                     "campos": {k: v for k, v in form.items() if k != "cauce_token_formulario"}})

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/_lab/e10/recibidos" and self._local():
            with lock:
                return self._json(200, {"recibidos": list(registro), "sin_token": modo["sin_token"]})
        return get0(self)

    def do_POST(self) -> None:
        if self.path.split("?")[0] == "/_lab/e10/modo" and self._local():
            try:
                body = json.loads(self._cuerpo() or b"{}")
            except ValueError:
                return self._json(400, {"detail": "JSON no válido"})
            modo["sin_token"] = body.get("sin_token") is True
            return self._json(200, dict(modo))
        return post0(self)

    H._recibir, H.do_GET, H.do_POST = _recibir, do_GET, do_POST
    srv = S.ServidorPortal(port=a.port, portal=S.Portal(capacidad=a.capacidad, por_minuto=a.por_minuto))
    srv.iniciar()
    print(f"portal E10 en {srv.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.detener()


if __name__ == "__main__":
    main()
