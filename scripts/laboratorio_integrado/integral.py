"""Validación integral del laboratorio de Cauce (7_VALIDACION_INTEGRAL.cmd).

Ejecuta, en orden y sin detenerse si una prueba falla, todo lo que se puede comprobar en el equipo, y pide ayuda a la
persona solo para lo físico (conectar el teléfono, desconectar el Wi‑Fi). Cada paso queda como:

  PASA       comprobado, con su tipo de evidencia
  FALLA      comprobado y no se cumple
  PENDIENTE  no se pudo comprobar porque falta un requisito físico o un destino autorizado (NUNCA cuenta como aprobado)
  OMITIDA    no aplica a este perfil o a esta rama
  INFO       medición sin criterio de aprobado

Tipos de evidencia: FÍSICA (Windows real, enlaces reales), LOCAL (en el equipo, sin cortar la red), SIMULADA.

Perfiles: rapido (~5 min, sin preguntas salvo el teléfono), completo (~25 min, con las pruebas físicas guiadas),
prolongado (1 h) y nocturno (8 h). Los prolongados solo usan la red SIMULADA del laboratorio: no generan tráfico a terceros.

Restricciones que se respetan: no toca rutas, adaptadores ni firewall; la rotación sigue apagada en modo real; solo
abre y cierra conexiones TCP al destino que eligió la persona (por omisión github.com:443) durante la prueba física guiada.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
from pathlib import Path
from typing import Any, Callable, Optional

import lab as L

PASA, FALLA, PENDIENTE, OMITIDA, INFO = "PASA", "FALLA", "PENDIENTE", "OMITIDA", "INFO"
FISICA, LOCAL, SIMULADA = "FÍSICA", "LOCAL", "SIMULADA"
PERFILES = {
    "rapido": {"desc": "comprobación básica", "estabilidad_s": 60, "corte_cada_s": 0, "fisica": False, "caliente": False},
    "completo": {"desc": "batería integral", "estabilidad_s": 600, "corte_cada_s": 120, "fisica": True, "caliente": True},
    "prolongado": {"desc": "supervisión de 1 hora", "estabilidad_s": 3600, "corte_cada_s": 300, "fisica": False,
                   "caliente": False},
    "nocturno": {"desc": "supervisión de 8 horas", "estabilidad_s": 8 * 3600, "corte_cada_s": 900, "fisica": False,
                 "caliente": False},
}
OBJETIVO_POR_OMISION = "github.com:443"       # elegido por Diego (2026-11-27): solo conexión TCP, sin datos
MUESTREO_S = 15.0


def mask_ip(ip: Optional[str]) -> Optional[str]:
    if not ip or str(ip).count(".") != 3:
        return ip
    return ".".join(str(ip).split(".")[:3]) + ".x"


class Integral:
    def __init__(self, lab: L.Lab, perfil: str, interactivo: bool, objetivo: Optional[str],
                 duracion_s: Optional[float] = None, preguntar: Callable[[str], str] = input):
        self.lab, self.perfil, self.cfg = lab, perfil, dict(PERFILES[perfil])
        if duracion_s is not None:
            self.cfg["estabilidad_s"] = duracion_s
        self.interactivo, self.objetivo, self._preguntar = interactivo, objetivo or OBJETIVO_POR_OMISION, preguntar
        self.inicio = time.time()
        self.pasos: list[dict] = []
        self.matriz: dict[str, dict] = {}
        self.intervenciones: list[str] = []
        self.metricas: dict[str, Any] = {}
        sello = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = lab.d / "informes" / f"integral-{sello}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.puertos_normales_antes: dict = {}
        self._ne_sys()

    # ------------------------------------------------------------------ utilidades
    def _ne_sys(self) -> None:
        if str(self.lab.ne) not in sys.path:
            sys.path.insert(0, str(self.lab.ne))

    def preguntar(self, msg: str) -> Optional[str]:
        """None si no hay nadie para contestar (modo no interactivo)."""
        if not self.interactivo:
            return None
        try:
            return self._preguntar(f"\n>>> {msg}\n    (Enter para continuar, «omitir» para saltar): ").strip().lower()
        except EOFError:
            return None

    def paso(self, pid: str, titulo: str, fn: Callable[[], dict]) -> dict:
        L.decir(f"\n[{pid}] {titulo} …")
        t0 = time.time()
        try:
            r = fn() or {}
        except Exception as e:  # noqa: BLE001  una prueba que revienta no detiene las demás
            r = {"estado": FALLA, "detalle": f"{type(e).__name__}: {e}",
                 "traza": traceback.format_exc(limit=4)[-1500:]}
        r = {"id": pid, "titulo": titulo, "estado": r.pop("estado", FALLA), "evidencia": r.pop("evidencia", LOCAL),
             "s": round(time.time() - t0, 1), **r}
        self.pasos.append(r)
        L.decir(f"[{pid}] {r['estado']} ({r['evidencia']}, {r['s']} s) {r.get('detalle', '')}")
        self.escribir()            # informe parcial tras cada paso: si algo se cuelga, queda lo hecho
        return r

    def ne_admin(self) -> Optional[str]:
        return self.lab.admin_ne()

    def ne(self, ruta: str, metodo: str = "GET", cuerpo=None):
        return L.http(f"http://127.0.0.1:{self.lab.p['network_engine']}/api/v1{ruta}", metodo, cuerpo, self.ne_admin())

    def descubrir(self) -> list:
        from cauce.discovery import default_provider
        from cauce.independence import analyze
        return analyze(default_provider().discover())

    @staticmethod
    def fisicas(conns) -> list:
        from cauce.models import Kind
        return [c for c in conns if not c.is_virtual and c.kind not in (Kind.VPN, Kind.VIRTUAL, Kind.LOOPBACK)]

    @staticmethod
    def resumen_if(c) -> dict:
        return {"id": c.id, "nombre": c.name, "tipo": c.kind.value, "enlace": c.link.value, "ip": mask_ip(c.primary_ip),
                "puerta_enlace": "sí" if c.gateway else "no", "descripcion": c.description[:60]}

    def mf(self, f: str, estado: str, detalle: str, evidencia: str) -> None:
        """Matriz F1–F12: se queda con la mejor evidencia (física > simulada) y nunca rebaja un fallo físico."""
        rango = {"Aprobado físicamente": 3, "Fallido": 2, "Aprobado en simulación": 1, "Pendiente": 0}
        act = self.matriz.get(f)
        if act is None or rango[estado] >= rango[act["estado"]]:
            self.matriz[f] = {"estado": estado, "detalle": detalle, "evidencia": evidencia}

    # ------------------------------------------------------------------ pasos
    def p01_versiones(self) -> dict:
        v = self.lab.comprobar_versiones()
        venvs = {k: Path(self.lab.py(k)) != Path(sys.executable) for k in ("monitor", "forms")}
        chromium = self.lab.navegadores.exists() and any(self.lab.navegadores.iterdir())
        return {"estado": PASA if v["ok"] else FALLA, "detalle": f"Python {v['python']}; entornos propios {venvs}; "
                f"Chromium de Forms {'sí' if chromium else 'no'}", "versiones": v["detalle"]}

    def p02_puertos(self) -> dict:
        p = self.lab.comprobar_puertos()
        self.puertos_normales_antes = {x["servicio"]: x["en_uso"] for x in p["instalaciones_normales"]}
        ocupados = [f"{x['puerto']} ({x['usado_por'] or 'otro programa'})" for x in p["detalle"]
                    if not x["libre"] and not x["del_laboratorio"]]
        return {"estado": PASA if p["ok"] else FALLA, "detalle": "libres" if p["ok"] else "ocupados: " + ", ".join(ocupados),
                "instalaciones_normales": p["instalaciones_normales"]}

    def p03_aislamiento(self) -> dict:
        problemas = []
        if not (self.lab.d / L.MARCA).exists():
            problemas.append("falta la marca del laboratorio")
        for x in L.prohibidas():
            if L._dentro(self.lab.d, x) or L._dentro(x, self.lab.d):
                problemas.append(f"carpeta solapada con {x}")
        if (self.lab.d / ".env").exists():
            problemas.append("hay un .env en la carpeta del laboratorio")
        cfg = json.loads(self.lab.datos("network-engine", "config.json").read_text("utf-8")) \
            if self.lab.datos("network-engine", "config.json").exists() else {}
        if cfg.get("lab", {}).get("rotation_enabled") not in (None, False):
            problemas.append("rotación activada en la configuración del laboratorio")
        return {"estado": FALLA if problemas else PASA, "detalle": "; ".join(problemas) or
                f"carpeta propia {self.lab.d}, sin .env, fuera de las instalaciones operativas, rotación apagada"}

    def p04_inicio(self) -> dict:
        self.lab.detener()
        r = self.lab.iniciar("simulado", None, abrir=False)
        return {"estado": PASA if r["ok"] else FALLA, "evidencia": LOCAL, "detalle": json.dumps(r.get("servicios") or r.get("motivo"))}

    def p05_interfaces(self) -> dict:
        conns = self.descubrir()
        fis = self.fisicas(conns)
        utiles = [c for c in fis if c.usable and c.gateway]
        self.metricas["interfaces"] = [self.resumen_if(c) for c in conns]
        ev = FISICA if L.WINDOWS else LOCAL
        return {"estado": PASA if utiles else FALLA, "evidencia": ev,
                "detalle": f"{len(conns)} adaptadores, {len(fis)} físicos, {len(utiles)} con salida: "
                           + ", ".join(f"{c.name} ({c.kind.value})" for c in utiles),
                "interfaces": self.metricas["interfaces"]}

    def _telefonos(self) -> list:
        from cauce.models import Kind
        return [c for c in self.descubrir() if c.kind in (Kind.TETHER, Kind.MOBILE)]

    def _esperar(self, cond: Callable[[], Any], limite: float, paso: float = 2.0):
        t0 = time.time()
        while time.time() - t0 < limite:
            r = cond()
            if r:
                return r, round(time.time() - t0, 1)
            time.sleep(paso)
        return None, round(time.time() - t0, 1)

    def p06_telefono(self) -> dict:
        tel = [c for c in self._telefonos() if c.usable and c.gateway]
        if tel:
            return {"estado": PASA, "evidencia": FISICA if L.WINDOWS else LOCAL,
                    "detalle": "teléfono ya conectado: " + ", ".join(f"{c.name} [{c.description[:40]}]" for c in tel),
                    "telefono": [self.resumen_if(c) for c in tel]}
        resp = self.preguntar("Conecta ahora el Samsung por USB y activa «Módem USB» en el teléfono (con su Wi‑Fi apagado).")
        if resp is None or resp == "omitir":
            self.intervenciones.append("Conectar el teléfono por USB en modo «Módem USB» y repetir la validación.")
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "no hay teléfono conectado por USB"}
        enc, s = self._esperar(lambda: [c for c in self._telefonos() if c.usable and c.gateway], 120)
        if not enc:
            return {"estado": FALLA, "evidencia": FISICA, "detalle": f"Windows no mostró un teléfono con salida en {s} s",
                    "vistos": [self.resumen_if(c) for c in self._telefonos()]}
        return {"estado": PASA, "evidencia": FISICA, "detalle": f"detectado en caliente en {s} s: {enc[0].name} "
                f"[{enc[0].description[:40]}]", "telefono": [self.resumen_if(c) for c in enc]}

    def p06b_caliente(self) -> dict:
        """Desconexión y reconexión física del teléfono: ¿lo ve desaparecer y volver? ¿cambia su identificador?"""
        if not self.cfg["caliente"]:
            return {"estado": OMITIDA, "detalle": "solo en el perfil completo"}
        tel = [c for c in self._telefonos() if c.usable]
        if not tel:
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "sin teléfono conectado"}
        antes = tel[0]
        if self.preguntar(f"Desconecta el cable USB del teléfono «{antes.name}».") in (None, "omitir"):
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "la persona omitió la prueba"}
        fuera, s1 = self._esperar(lambda: not any(c.id == antes.id and c.usable for c in self._telefonos()), 60)
        if self.preguntar("Vuelve a conectar el cable (puedes usar OTRO puerto USB) y activa «Módem USB».") in (None, "omitir"):
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "la persona omitió la reconexión"}
        dentro, s2 = self._esperar(lambda: [c for c in self._telefonos() if c.usable and c.gateway], 120)
        if not (fuera and dentro):
            return {"estado": FALLA, "evidencia": FISICA, "detalle": f"desaparición {'sí' if fuera else 'no'} ({s1} s), "
                    f"reaparición {'sí' if dentro else 'no'} ({s2} s)"}
        nuevo = dentro[0]
        return {"estado": PASA, "evidencia": FISICA,
                "detalle": f"desaparece en {s1} s, vuelve en {s2} s; identificador {antes.id} -> {nuevo.id}"
                           f"{' (cambió: Network Engine lo reconoce por la MAC)' if nuevo.id != antes.id else ''}"}

    def p07_diagnostico(self) -> dict:
        d = self.lab.diagnostico()
        return {"estado": PASA if d["ok"] else FALLA, "evidencia": SIMULADA,
                "detalle": f"NE {d.get('network_engine', {}).get('mode')}, Monitor {d.get('monitor', {}).get('pantalla_red')}, "
                           f"rutas globales modificadas: {d.get('network_engine', {}).get('global_routes_modified')}"}

    def p08a_interrupcion_simulada(self) -> dict:
        r = self.lab.prueba_interrupcion(240)
        return {"estado": PASA if r["ok"] else FALLA, "evidencia": SIMULADA,
                "detalle": "; ".join(f"{p['paso']}: {p.get('s', '')}" for p in r.get("pasos", []))}

    def p08b_guion_simulado(self) -> dict:
        """F1–F12 con el simulador (sin red): evidencia SIMULADA de conmutación y escrituras exactamente una vez."""
        out = self.dir / "fieldcheck-simulado"
        r = subprocess.run([sys.executable, "-m", "cauce", "fieldcheck", "--lab-url", "http://lab.sim:8770", "--simulate",
                            "--engines", "lab", "--lab-port-offset", str(L.desplazamiento_puertos()), "--out", str(out), "--steps", "120"], cwd=str(self.lab.ne),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=L.entorno_limpio({}))
        res = json.loads((out / "fieldcheck.json").read_text("utf-8")) if (out / "fieldcheck.json").exists() else {}
        for c in res.get("checks", []):
            if c["status"] == "ok":
                self.mf(c["id"], "Aprobado en simulación", c["detail"], "fieldcheck --simulate")
            elif c["status"] == "fallo":
                self.mf(c["id"], "Fallido", c["detail"], "fieldcheck --simulate")
        return {"estado": PASA if r.returncode == 0 else FALLA, "evidencia": SIMULADA,
                "detalle": f"fallos: {res.get('failed') or 'ninguno'}", "informe": str(out / "fieldcheck.md")}

    def p08c_fisica(self) -> dict:
        """Corte REAL guiado: Network Engine del laboratorio en modo real (solo observa) y Monitor mirando."""
        if not self.cfg["fisica"]:
            self.intervenciones.append("Para las pruebas físicas de corte: perfil «completo».")
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": f"el perfil {self.perfil} no incluye el corte físico"}
        fis = [c for c in self.fisicas(self.descubrir()) if c.usable and c.gateway]
        if len({c.gateway for c in fis}) < 2:
            self.intervenciones.append("Conectar un segundo enlace (teléfono por USB) para F2, F3, F6 y F7.")
            out = self.dir / "fieldcheck-fisico"
            subprocess.run([sys.executable, "-m", "cauce", "fieldcheck", "--objetivo", self.objetivo, "--engines", "lab", "--lab-port-offset", str(L.desplazamiento_puertos()),
                            "--out", str(out)], cwd=str(self.lab.ne), env=L.entorno_limpio({}), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            self._matriz_fieldcheck(out, FISICA)
            return {"estado": PENDIENTE, "evidencia": FISICA,
                    "detalle": f"{len(fis)} enlace(s) con salida: hace falta un segundo (F1, F10–F12 sí se midieron)"}
        if not self.interactivo:
            self.intervenciones.append("Ejecutar el perfil completo en persona para desconectar y reconectar el Wi‑Fi.")
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "hace falta una persona para desconectar el enlace"}
        if self.preguntar(f"Prueba de corte REAL. Solo se abrirán conexiones TCP a {self.objetivo}. Cuando se pida, "
                          "desconecta el enlace principal (Wi‑Fi desde la barra de tareas) y vuelve a conectarlo.") == "omitir":
            return {"estado": PENDIENTE, "evidencia": FISICA, "detalle": "la persona omitió la prueba"}
        # Network Engine del laboratorio pasa a modo real (solo observa ese destino) para que Monitor vea el corte real
        self.lab.detener("network_engine")
        self.lab.estado["modo"], self.lab.estado["objetivo"] = "real", self.objetivo
        self.lab._cfg_ne("real", None, self.objetivo)
        self.lab.iniciar_ne()
        tok = self.lab.monitor_token()
        vistos: list[dict] = []
        parar = threading.Event()

        def vigilar():
            while not parar.is_set():
                r = self.lab.red(tok) if tok else {}
                for e in r.get("eventos") or []:
                    if e.get("tipo") in ("perdida", "recuperada") and e not in vistos:
                        vistos.append({**e, "visto": time.time()})
                parar.wait(2)
        th = threading.Thread(target=vigilar, daemon=True)
        th.start()
        out = self.dir / "fieldcheck-fisico"
        try:
            r = subprocess.run([sys.executable, "-m", "cauce", "fieldcheck", "--objetivo", self.objetivo, "--engines", "lab", "--lab-port-offset", str(L.desplazamiento_puertos()),
                                "--out", str(out)], cwd=str(self.lab.ne), env=L.entorno_limpio({}))
        finally:
            time.sleep(20)
            parar.set()
            th.join(5)
            self.lab.detener("network_engine")
            self.lab.estado["modo"], self.lab.estado["objetivo"] = "simulado", None
            self.lab._cfg_ne("simulado", None)
            self.lab.iniciar_ne()
        res = self._matriz_fieldcheck(out, FISICA)
        tipos = sorted({e.get("tipo") for e in vistos})
        return {"estado": PASA if r.returncode == 0 else FALLA, "evidencia": FISICA,
                "detalle": f"fallos del guion: {res.get('failed') or 'ninguno'}; Monitor mostró: {tipos or 'nada'}",
                "monitor_eventos": vistos[-6:], "informe": str(out / "fieldcheck.md")}

    def _matriz_fieldcheck(self, out: Path, ev: str) -> dict:
        res = json.loads((out / "fieldcheck.json").read_text("utf-8")) if (out / "fieldcheck.json").exists() else {}
        fisico = ev == FISICA and "FÍSICA" in str(res.get("evidence_type", "")) and "OTRO SISTEMA" not in str(res.get("evidence_type", ""))
        for c in res.get("checks", []):
            if c["id"].startswith("F") and "/" not in c["id"] and "-" not in c["id"]:
                if c["status"] == "ok":
                    self.mf(c["id"], "Aprobado físicamente" if fisico else "Aprobado en simulación", c["detail"],
                            "fieldcheck " + ("físico" if fisico else str(res.get("evidence_type"))))
                elif c["status"] == "fallo":
                    self.mf(c["id"], "Fallido", c["detail"], "fieldcheck físico")
        return res

    def p09_navegador(self) -> dict:
        r = self.lab.prueba_forms()
        if r.get("omitida"):
            self.intervenciones.append("Instalar Chromium de Forms: 1_INSTALAR.cmd --navegador-forms.")
            return {"estado": PENDIENTE, "evidencia": LOCAL, "detalle": r["motivo"]}
        return {"estado": PASA if r["ok"] else FALLA, "evidencia": LOCAL,
                "detalle": "; ".join(f"{p['paso']}: {'ok' if p['ok'] else 'NO'}" for p in r.get("pasos", []))}

    def p09b_sesiones(self) -> dict:
        if not (self.lab.ne / "cauce" / "sessions.py").exists():
            return {"estado": OMITIDA, "detalle": "esta rama de Network Engine no incluye sesiones de navegador (PR #10)"}
        try:
            import sesion_laboratorio  # noqa: F401  (rama de sesiones)
        except ImportError:
            return {"estado": OMITIDA, "detalle": "falta scripts/laboratorio_integrado/sesion_laboratorio.py"}
        return sesion_laboratorio.prueba(self)

    def p10_reinicios(self) -> dict:
        det = {}
        r = self.lab.prueba_ne_apagado()
        det["network_engine"] = {"ok": r["ok"], **{p["paso"]: p.get("s") for p in r.get("pasos", [])}}
        for nombre, iniciar in (("monitor", self.lab.iniciar_monitor), ("forms", self.lab.iniciar_forms)):
            if nombre == "forms" and not (self.lab.estado.get("forms", True) and self.lab.forms.exists()):
                continue
            t0 = time.time()
            self.lab.detener(nombre)
            otros = {k: not L.puerto_libre(self.lab.p[k]) for k in ("network_engine", "monitor", "forms") if k != nombre}
            vuelve = iniciar()
            extra = {}
            if nombre == "monitor" and vuelve:
                tok = self.lab.monitor_token()
                ok, s, _ = self.lab.esperar_red(tok, lambda r: r.get("network_engine") == "disponible", 90) if tok else (False, 0, {})
                extra = {"lee_ne_en_s": s if ok else None}
                vuelve = vuelve and ok
            det[nombre] = {"ok": vuelve and all(otros.values()), "los_otros_siguen": otros,
                           "s": round(time.time() - t0, 1), **extra}
        ok = all(v["ok"] for v in det.values())
        return {"estado": PASA if ok else FALLA, "evidencia": LOCAL, "detalle": json.dumps(det, ensure_ascii=False)}

    def p11_consistencia(self) -> dict:
        st, c = self.ne("/status/connectivity")
        st2, d = self.ne("/diagnostics")
        tok = self.lab.monitor_token()
        red = self.lab.red(tok) if tok else {}
        if st != 200 or not red:
            return {"estado": FALLA, "detalle": "Network Engine o Monitor no responden"}
        ne_c = sorted(c.get("connections") or [], key=lambda x: str(x.get("id")))
        mon = red.get("interfaces") or []
        problemas = []
        if len(ne_c) != len(mon):
            problemas.append(f"Network Engine ve {len(ne_c)} interfaces y Monitor {len(mon)}")
        for a, b in zip(ne_c, mon):
            if b.get("disponible") and a.get("status") not in (None, "ok", "degraded"):
                problemas.append(f"Monitor muestra disponible una interfaz que Network Engine da por {a.get('status')}")
            if b.get("disponible") and a.get("stale"):
                problemas.append("Monitor muestra disponible una medición vieja")
        if isinstance(d, dict) and d.get("global_routes_modified") is not False:
            problemas.append("Network Engine no confirma que no toca las rutas globales")
        return {"estado": FALLA if problemas else PASA, "evidencia": SIMULADA,
                "detalle": "; ".join(problemas) or f"{len(ne_c)} interfaces coinciden; ninguna sana sin respaldo de NE"}

    def p12_auditoria(self) -> dict:
        rutas = [self.lab.datos("network-engine", "audit.jsonl")] + sorted(self.dir.glob("fieldcheck-*/engine-data/audit.jsonl"))
        det = {}
        for p in rutas:
            if not p.exists():
                continue
            r = subprocess.run([sys.executable, "-m", "cauce", "verify-audit", str(p)], cwd=str(self.lab.ne),
                               capture_output=True, text=True, encoding="utf-8", errors="replace", env=L.entorno_limpio({}))
            det[str(p.relative_to(self.lab.d))] = r.returncode == 0
        if not det:
            return {"estado": FALLA, "detalle": "no hay registros de auditoría"}
        ok = all(det.values())
        if ok:
            self.mf("F10", "Aprobado en simulación", "cadena íntegra tras reinicios forzados", "auditoría del laboratorio")
        return {"estado": PASA if ok else FALLA, "detalle": json.dumps(det, ensure_ascii=False)}

    def p13_seguridad(self) -> dict:
        det, problemas = {}, []
        # 1) credenciales protegidas en disco
        for nombre in ("network-engine/api.token", "monitor.token"):
            f = self.lab.d / "datos" / nombre
            if f.exists():
                cab = f.read_bytes()[:13]
                det[nombre] = cab.decode("ascii", "replace").strip()
                if L.WINDOWS and not cab.startswith(b"CAUCE-DPAPI1"):
                    problemas.append(f"{nombre} no está cifrado con DPAPI")
        # 2) 403: destino que no es de laboratorio -> rechazado SIN tráfico
        st, b = self.ne("/tasks", "POST", {"owner": "integral", "name": "destino-ajeno",
                                           "steps": [{"method": "GET", "url": "http://example.com/"}]})
        det["destino_ajeno"] = st
        if st != 403:
            problemas.append(f"un destino ajeno no se rechazó (HTTP {st})")
        # 3) 429: el servicio pide limitar -> la tarea se detiene, sin reintentos ni conmutación
        st, t = self.ne("/tasks", "POST", {"owner": "integral", "name": "limite",
                                           "steps": [{"method": "GET", "url": "http://lab.sim/limit"}]})
        if st == 202 and isinstance(t, dict):
            fin, _ = self._esperar(lambda: (lambda x: x if x[1] and x[1].get("state") not in ("pending", "running") else None)(
                self.ne(f"/tasks/{t['id']}")), 30, 0.5)
            tarea = fin[1] if fin else {}
            det["limite_429"] = {"estado": tarea.get("state"), "resultados": len(tarea.get("results") or [])}
            if tarea.get("state") != "throttled" or len(tarea.get("results") or []) != 1:
                problemas.append("un 429 no detuvo la tarea al primer intento")
        else:
            problemas.append(f"no se pudo crear la tarea de prueba del 429 (HTTP {st})")
        # 4) permisos mínimos
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin()) if L.WINDOWS else os.geteuid() == 0
        except Exception:  # noqa: BLE001
            admin = None
        det["administrador"] = admin
        # 5) persistencia de registros
        audit = self.lab.datos("network-engine", "audit.jsonl")
        det["auditoria_bytes"] = audit.stat().st_size if audit.exists() else 0
        if not det["auditoria_bytes"]:
            problemas.append("la auditoría no se está guardando")
        return {"estado": FALLA if problemas else PASA, "evidencia": SIMULADA,
                "detalle": "; ".join(problemas) or "credenciales protegidas; 403 sin tráfico; 429 detiene la tarea; "
                f"{'ejecutado SIN privilegios de administrador' if admin is False else 'ATENCIÓN: con privilegios de administrador (no hace falta)'}",
                "datos": det}

    # ------------------------------------------------------------------ estabilidad
    def _procesos(self) -> dict[str, dict]:
        """RAM (MB) y CPU (s) del árbol de procesos de cada motor del laboratorio (incluye el hijo del venv en Windows)."""
        pids = dict(self.lab.estado.get("pids") or {})
        out: dict[str, dict] = {}
        if L.WINDOWS:
            ps = ("$s = @{}; Get-NetTCPConnection -ErrorAction SilentlyContinue | ForEach-Object { $s[[int]$_.OwningProcess]++ }; "
                  "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Select-Object ProcessId,ParentProcessId,"
                  "WorkingSetSize,KernelModeTime,UserModeTime,ThreadCount,HandleCount,"
                  "@{n='Sockets';e={[int]$s[[int]$_.ProcessId]}} | ConvertTo-Json -Compress")
            try:
                raw = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                                     text=True, timeout=30, creationflags=0x08000000).stdout.strip()
                filas = json.loads(raw) if raw else []
                filas = filas if isinstance(filas, list) else [filas]
            except Exception:  # noqa: BLE001
                filas = []
            hijos: dict[int, list] = {}
            for f in filas:
                hijos.setdefault(f.get("ParentProcessId"), []).append(f)
            por_pid = {f.get("ProcessId"): f for f in filas}
            for nombre, pid in pids.items():
                arbol, pend = [], [pid]
                while pend:
                    x = pend.pop()
                    if x in por_pid:
                        arbol.append(por_pid[x])
                    pend += [h.get("ProcessId") for h in hijos.get(x, [])]
                if arbol:
                    out[nombre] = {"vivo": True, "ram_mb": round(sum(f.get("WorkingSetSize") or 0 for f in arbol) / 2**20, 1),
                                   "cpu_s": round(sum((f.get("KernelModeTime") or 0) + (f.get("UserModeTime") or 0)
                                                      for f in arbol) / 1e7, 2),
                                   "procesos": len(arbol), "hilos": sum(f.get("ThreadCount") or 0 for f in arbol),
                                   "handles": sum(f.get("HandleCount") or 0 for f in arbol),
                                   "sockets": sum(f.get("Sockets") or 0 for f in arbol)}
                else:
                    out[nombre] = {"vivo": False}
            return out
        tick, pagina = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
        stats = {}
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    campos = (d / "stat").read_text().rsplit(")", 1)[1].split()
                    rss = int((d / "statm").read_text().split()[1]) * pagina
                    hilos = int(campos[17])
                    stats[int(d.name)] = (int(campos[1]), (int(campos[11]) + int(campos[12])) / tick, rss, hilos, d)
                except (OSError, IndexError, ValueError):
                    pass
        for nombre, pid in pids.items():
            arbol, pend = [], [pid]
            while pend:
                x = pend.pop()
                if x in stats:
                    arbol.append(stats[x])
                    pend += [p for p, s in stats.items() if s[0] == x]
            fds = socks = 0
            for a in arbol:
                try:
                    for f in (a[4] / "fd").iterdir():
                        fds += 1
                        try:
                            socks += os.readlink(f).startswith("socket:")
                        except OSError:
                            pass
                except OSError:
                    pass
            out[nombre] = ({"vivo": True, "ram_mb": round(sum(a[2] for a in arbol) / 2**20, 1),
                            "cpu_s": round(sum(a[1] for a in arbol), 2), "procesos": len(arbol),
                            "hilos": sum(a[3] for a in arbol), "handles": fds, "sockets": socks} if arbol else {"vivo": False})
        return out

    def _tamanos(self) -> dict[str, int]:
        out = {}
        for p in (self.lab.d / "datos").rglob("*"):
            if p.is_file() and p.suffix in (".jsonl", ".log", ".db", ".sqlite"):
                out[str(p.relative_to(self.lab.d))] = p.stat().st_size
        return out

    def _corte_simulado(self, tok: str) -> dict:
        adm = self.ne_admin()
        base = f"http://127.0.0.1:{self.lab.p['network_engine']}/api/v1/sim/connections/sim-a/state"
        L.http(base, "POST", {"state": "down"}, adm)
        ok1, s1, _ = self.lab.esperar_red(tok, lambda r: any(i.get("salud") not in ("SANA",) and not i.get("disponible")
                                                             for i in (r.get("interfaces") or [])[:1]), 90)
        L.http(base, "POST", {"state": "up"}, adm)
        ok2, s2, _ = self.lab.esperar_red(tok, lambda r: bool((r.get("interfaces") or [{}])[0].get("disponible")), 150)
        return {"deteccion_s": s1 if ok1 else None, "recuperacion_s": s2 if ok2 else None}

    def p14_estabilidad(self) -> dict:
        dur, cada = float(self.cfg["estabilidad_s"]), float(self.cfg["corte_cada_s"])
        tok = self.lab.monitor_token()
        if not tok:
            return {"estado": FALLA, "detalle": "Monitor no responde"}
        L.decir(f"  Supervisión durante {dur / 60:.0f} min (red SIMULADA, sin tráfico a terceros); muestras cada {MUESTREO_S:.0f} s.")
        muestras, cortes, errores_http = [], [], 0
        t0 = time.time()
        prox_corte = t0 + cada if cada else float("inf")
        tam0 = self._tamanos()
        err0 = self._errores()
        pids0 = dict(self.lab.estado.get("pids") or {})
        sesion = self._sesion_supervision()
        parcial = self.dir / "muestras_estabilidad.jsonl"            # se conserva aunque la prueba se interrumpa
        while time.time() - t0 < dur:
            m = {"t": round(time.time() - t0, 1), "procesos": self._procesos(), "responde": {}}
            st, c = self.ne("/status/connectivity")
            m["responde"]["network_engine"] = st == 200
            if st != 200:
                errores_http += 1
            else:
                m["latencia_ms"] = {x["id"]: x.get("avg_latency_ms") for x in c.get("connections") or []}
                m["disponibilidad_pct"] = {x["id"]: x.get("availability_pct") for x in c.get("connections") or []}
            red = self.lab.red(tok)
            m["responde"]["monitor"] = bool(red)
            if not red:
                errores_http += 1
                tok = self.lab.monitor_token() or tok
            if self.lab.estado.get("forms", True) and self.lab.forms.exists():
                m["responde"]["forms"] = not L.puerto_libre(self.lab.p["forms"]) and self._http_ok(self.lab.p["forms"])
            if sesion:
                m["sesion"] = self._latir_sesion(sesion)
            m["pids_cambiados"] = sorted(k for k, v in (self.lab.estado.get("pids") or {}).items() if pids0.get(k) != v)
            muestras.append(m)
            try:
                with open(parcial, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(m, ensure_ascii=False) + "\n")
            except OSError:
                pass
            if time.time() >= prox_corte:
                cortes.append(self._corte_simulado(tok))
                prox_corte = time.time() + cada
            restante = dur - (time.time() - t0)
            if restante > 0:
                time.sleep(min(MUESTREO_S, restante))
            if len(muestras) % 20 == 0:
                L.decir(f"  … {m['t'] / 60:.0f} min, {len(cortes)} cortes simulados")
        tam1 = self._tamanos()
        if sesion:
            self._cerrar_sesion(sesion)
        resumen = self._resumen_estabilidad(muestras, cortes, errores_http, tam0, tam1, time.time() - t0, err0)
        resumen["auditoria_integra_al_final"] = self._auditoria_integra()
        self.metricas["estabilidad"] = resumen
        (self.dir / "muestras_estabilidad.json").write_text(json.dumps(muestras, ensure_ascii=False), "utf-8")
        problemas = resumen["problemas"]
        if resumen["auditoria_integra_al_final"] is False:
            problemas.append("la auditoría no está íntegra al terminar la supervisión")
        prev = resumen["errores"]["previos"]
        aviso = (f"; AVISO: {prev['monitor_trazas']} trazas y {prev['network_engine']} errores de NE de pasos anteriores "
                 f"(última: {resumen['errores'].get('ultima_traza')}); revisar datos/logs") if any(prev.values()) else ""
        return {"estado": FALLA if problemas else PASA, "evidencia": SIMULADA,
                "detalle": ("; ".join(problemas) or resumen["texto"]) + aviso}

    @staticmethod
    def _http_ok(puerto: int) -> bool:
        try:
            with L._OPENER.open(f"http://127.0.0.1:{puerto}/", timeout=5) as r:
                return r.status < 500
        except urllib.error.HTTPError as e:
            return e.code < 500
        except OSError:
            return False

    def _sesion_supervision(self) -> Optional[dict]:
        """Sesión de diagnóstico SIN navegador que late durante la supervisión, para medir su continuidad a lo largo de
        horas (fases, intervención humana). No envía nada; solo latidos de estado. None si NE no tiene sesiones."""
        if not (self.lab.ne / "cauce" / "sessions.py").exists():
            return None
        st, tok = self.ne("/tokens", "POST", {"name": "lab-supervision", "scope": "session"})
        if st not in (200, 201) or not isinstance(tok, dict):
            return None
        base = f"http://127.0.0.1:{self.lab.p['network_engine']}/api/v1"
        st, v = L.http(base + "/sessions", "POST", {"label": "lab-supervision", "connection_id": "sim-a",
                                                     "authorized_by_user": True, "heartbeat_timeout_s": 300}, tok["token"])
        if st != 201:
            self.ne(f"/tokens/{tok['id']}", "DELETE")
            return None
        return {"id": v["id"], "token": tok["token"], "token_id": tok["id"], "base": base}

    def _latir_sesion(self, s: dict) -> dict:
        st, v = L.http(f"{s['base']}/sessions/{s['id']}/heartbeat", "POST", {"status": "ok", "operation": "idle"}, s["token"])
        if st == 404:                                   # NE se reinició (sesiones en memoria): se registra de nuevo
            st2, v2 = L.http(s["base"] + "/sessions", "POST", {"label": "lab-supervision", "connection_id": "sim-a",
                                                               "authorized_by_user": True, "heartbeat_timeout_s": 300}, s["token"])
            if st2 == 201:
                s["id"] = v2["id"]
            return {"fase": None, "http": st, "re_registrada": st2 == 201}
        return {"fase": (v or {}).get("phase"), "estado": (v or {}).get("state"), "http": st,
                "requiere_persona": (v or {}).get("requires_human")}

    def _cerrar_sesion(self, s: dict) -> None:
        L.http(f"{s['base']}/sessions/{s['id']}", "DELETE", None, s["token"])
        self.ne(f"/tokens/{s['token_id']}", "DELETE")

    def _auditoria_integra(self) -> Optional[bool]:
        p = self.lab.datos("network-engine", "audit.jsonl")
        if not p.exists():
            return None
        r = subprocess.run([sys.executable, "-m", "cauce", "verify-audit", str(p)], cwd=str(self.lab.ne),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=L.entorno_limpio({}))
        return r.returncode == 0

    @staticmethod
    def _pendiente_mb_h(serie: list[tuple[float, float]]) -> Optional[float]:
        if len(serie) < 3:
            return None
        n = len(serie)
        mx, my = sum(x for x, _ in serie) / n, sum(y for _, y in serie) / n
        den = sum((x - mx) ** 2 for x, _ in serie)
        return round(sum((x - mx) * (y - my) for x, y in serie) / den * 3600, 2) if den else None

    def _resumen_estabilidad(self, muestras, cortes, errores_http, tam0, tam1, dur, err0=None) -> dict:
        problemas, procesos = [], {}
        nombres = {n for m in muestras for n in m["procesos"]}
        for n in sorted(nombres):
            serie = [(m["t"], m["procesos"][n]["ram_mb"]) for m in muestras if m["procesos"].get(n, {}).get("vivo")]
            caidas = sum(1 for m in muestras if not m["procesos"].get(n, {}).get("vivo", False))
            cpu = [m["procesos"][n]["cpu_s"] for m in muestras if m["procesos"].get(n, {}).get("vivo")]
            ram = [y for _, y in serie]
            pend = self._pendiente_mb_h(serie)
            crec = (ram[-1] - ram[0]) / ram[0] * 100 if len(ram) > 1 and ram[0] else 0.0
            fuga = None
            if dur >= 1800 and pend is not None:
                fuga = pend > 10 and crec > 25
                if fuga:
                    problemas.append(f"posible fuga de memoria en {n}: {pend} MB/h, +{crec:.0f} %")
            if caidas:
                problemas.append(f"{n} no estaba vivo en {caidas} muestras")
            vivos = [m for m in muestras if m["procesos"].get(n, {}).get("vivo")]
            # CPU por intervalo entre muestras consecutivas (% de un núcleo); el máximo detecta picos
            cpu_int = [max(0.0, (b["procesos"][n]["cpu_s"] - a["procesos"][n]["cpu_s"]) / (b["t"] - a["t"]) * 100)
                       for a, b in zip(vivos, vivos[1:]) if b["t"] > a["t"]]

            def serie_de(k):
                return [m["procesos"][n].get(k) for m in vivos if isinstance(m["procesos"][n].get(k), (int, float))]
            hilos, socks, procs = serie_de("hilos"), serie_de("sockets"), serie_de("procesos")
            procesos[n] = {"ram_mb_inicio": ram[0] if ram else None, "ram_mb_fin": ram[-1] if ram else None,
                           "ram_mb_media": round(sum(ram) / len(ram), 1) if ram else None,
                           "ram_mb_max": max(ram) if ram else None, "pendiente_mb_h": pend,
                           "fuga": "sin datos suficientes (< 30 min)" if fuga is None else fuga,
                           "cpu_pct_medio": round((cpu[-1] - cpu[0]) / dur * 100, 2) if len(cpu) > 1 and dur else None,
                           "cpu_pct_max_intervalo": round(max(cpu_int), 1) if cpu_int else None,
                           "procesos_max": max(procs) if procs else None,
                           "hilos_inicio": hilos[0] if hilos else None, "hilos_max": max(hilos) if hilos else None,
                           "hilos_fin": hilos[-1] if hilos else None,
                           "sockets_inicio": socks[0] if socks else None, "sockets_max": max(socks) if socks else None,
                           "sockets_fin": socks[-1] if socks else None,
                           "muestras_sin_proceso": caidas}
            if dur >= 1800 and len(hilos) > 10 and hilos[-1] > hilos[0] * 2 + 10:
                problemas.append(f"los hilos de {n} crecen sin parar: {hilos[0]} → {hilos[-1]}")
            if dur >= 1800 and len(socks) > 10 and socks[-1] > socks[0] * 2 + 20:
                problemas.append(f"los sockets de {n} crecen sin parar: {socks[0]} → {socks[-1]}")
        lat = [v for m in muestras for v in (m.get("latencia_ms") or {}).values() if isinstance(v, (int, float))]
        det = [c["deteccion_s"] for c in cortes if c["deteccion_s"] is not None]
        rec = [c["recuperacion_s"] for c in cortes if c["recuperacion_s"] is not None]
        if len(det) < len(cortes) or len(rec) < len(cortes):
            problemas.append(f"{len(cortes) - len(det)} cortes sin detectar o {len(cortes) - len(rec)} sin recuperar a tiempo")
        crecimiento = {k: tam1.get(k, 0) - tam0.get(k, 0) for k in tam1}
        por_hora = {k: round(v / max(dur, 1) * 3600 / 1024, 1) for k, v in crecimiento.items() if v}
        fin = self._errores()
        ini = err0 or {}
        # solo cuenta lo que aparece DURANTE la supervisión; lo anterior (p. ej. al apagar NE en el paso 10) se informa aparte
        n_err = {k: fin[k] - ini.get(k, 0) for k in ("network_engine", "monitor_trazas")}
        n_err["previos"] = {k: ini.get(k, 0) for k in ("network_engine", "monitor_trazas")}
        n_err["ultima_traza"] = fin.get("ultima_traza")
        if n_err["network_engine"] or n_err["monitor_trazas"]:
            problemas.append(f"errores durante la supervisión: NE {n_err['network_engine']}, trazas {n_err['monitor_trazas']}"
                             + (f" (última: {fin['ultima_traza']})" if fin.get("ultima_traza") else ""))
        if errores_http > max(2, len(muestras) // 20):
            problemas.append(f"{errores_http} consultas sin respuesta")
        disponibilidad = {}
        for k in ("network_engine", "monitor", "forms"):
            vals = [m["responde"][k] for m in muestras if k in (m.get("responde") or {})]
            if vals:
                disponibilidad[k] = round(sum(vals) / len(vals) * 100, 2)
        fases: dict[str, int] = {}
        for m in muestras:
            f = (m.get("sesion") or {}).get("fase")
            if f:
                fases[f] = fases.get(f, 0) + 1
        sesion_persona = sum(1 for m in muestras if (m.get("sesion") or {}).get("requiere_persona"))
        if sesion_persona:
            problemas.append(f"la sesión de supervisión (sin operación en curso) pidió intervención humana en {sesion_persona} muestras")
        reinicios = sorted({k for m in muestras for k in m.get("pids_cambiados") or []})
        if reinicios:
            problemas.append(f"reinicios inesperados de: {', '.join(reinicios)}")
        texto = (f"{len(muestras)} muestras en {dur / 60:.1f} min; {len(cortes)} cortes simulados, detección media "
                 f"{(sum(det) / len(det)) if det else '-'} s, recuperación media {(sum(rec) / len(rec)) if rec else '-'} s; "
                 f"RAM final " + ", ".join(f"{k} {v['ram_mb_fin']} MB" for k, v in procesos.items()))
        return {"duracion_min": round(dur / 60, 1), "muestras": len(muestras), "procesos": procesos,
                "latencia_ms": {"media": round(sum(lat) / len(lat), 1) if lat else None, "max": max(lat) if lat else None},
                "cortes": cortes, "registros_kb_por_hora": por_hora, "errores": n_err, "consultas_sin_respuesta": errores_http,
                "disponibilidad_motores_pct": disponibilidad, "sesion_fases": fases, "reinicios_inesperados": reinicios,
                "deteccion_s": {"media": round(sum(det) / len(det), 1) if det else None, "max": max(det) if det else None},
                "recuperacion_s": {"media": round(sum(rec) / len(rec), 1) if rec else None, "max": max(rec) if rec else None},
                "problemas": problemas, "texto": texto}

    def _errores(self) -> dict:
        ne = 0
        audit = self.lab.datos("network-engine", "audit.jsonl")
        if audit.exists():
            with open(audit, encoding="utf-8", errors="replace") as fh:
                ne = sum(1 for ln in fh if '"kind": "error"' in ln)
        trazas, ultima = 0, None
        for p in sorted((self.lab.d / "datos" / "logs").glob("*.log")):
            txt = p.read_text("utf-8", errors="replace")
            n = txt.count("Traceback (most recent call last)")
            if n:
                trazas += n
                # última línea de la última traza (tipo de excepción), recortada: sirve para diagnosticar sin volcar el registro
                cola = [ln.strip() for ln in txt.rsplit("Traceback (most recent call last)", 1)[1].splitlines()[1:]
                        if ln.strip() and not ln.startswith((" ", "\t"))]
                ultima = f"{p.name}: {(cola[0] if cola else '?')[:160]}"
        return {"network_engine": ne, "monitor_trazas": trazas, "ultima_traza": ultima}

    # ------------------------------------------------------------------ cierre e informe
    def p15_cierre(self) -> dict:
        r = self.lab.detener()
        libres = {n: L.puerto_libre(p) for n, p in self.lab.p.items()}
        despues = {k: not L.puerto_libre(v) for k, v in {"Monitor normal": 8000, "Forms normal": 8765,
                                                         "Network Engine 0.2": 8780}.items()}
        cambios = {k: (self.puertos_normales_antes.get(k), v) for k, v in despues.items()
                   if self.puertos_normales_antes and self.puertos_normales_antes.get(k) != v}
        ok = all(libres.values()) and not cambios
        return {"estado": PASA if ok else FALLA, "detalle": f"{r}; puertos del laboratorio libres: {all(libres.values())}; "
                f"instalaciones normales sin cambios: {not cambios}" + (f" {cambios}" if cambios else "")}

    def p16_datos_sensibles(self) -> dict:
        secretos = [s for s in (self.lab.estado.get("monitor_admin"),) if s]
        for nombre in ("network-engine/api.token", "monitor.token"):
            f = self.lab.d / "datos" / nombre
            try:
                from cauce.secrets_store import read_secret
                secretos.append(read_secret(str(f)))
            except Exception:  # noqa: BLE001
                pass
        mac = re.compile(r"\b[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}\b")
        hallazgos = []
        for p in list((self.lab.d / "informes").rglob("*")):
            if p.is_file() and p.suffix in (".md", ".json", ".log", ".txt") and "engine-data" not in p.parts:
                t = p.read_text("utf-8", errors="replace")
                if any(s and s in t for s in secretos):
                    hallazgos.append(f"{p.name}: contiene una credencial")
                if mac.search(t):
                    hallazgos.append(f"{p.name}: contiene una MAC")
        return {"estado": FALLA if hallazgos else PASA,
                "detalle": "; ".join(hallazgos) or "informes sin contraseñas, tokens ni MAC (IP enmascaradas)"}

    def escribir(self) -> Path:
        cuenta = {e: sum(1 for p in self.pasos if p["estado"] == e) for e in (PASA, FALLA, PENDIENTE, OMITIDA, INFO)}
        filas = [f"| {p['id']} | {p['titulo']} | **{p['estado']}** | {p['evidencia']} | {p['s']} | "
                 f"{str(p.get('detalle', '')).replace('|', '/')[:300]} |" for p in self.pasos]
        todos = [f"F{i}" for i in range(1, 13)]
        mfilas = [f"| {f} | {self.matriz.get(f, {}).get('estado', 'Pendiente')} | "
                  f"{str(self.matriz.get(f, {}).get('detalle', '—')).replace('|', '/')[:160]} | "
                  f"{self.matriz.get(f, {}).get('evidencia', '—')} |" for f in todos]
        lin = ["# Validación integral de Cauce", "",
               f"- Perfil: **{self.perfil}** ({self.cfg['desc']}) · interactivo: {'sí' if self.interactivo else 'no'} · "
               f"inicio {dt.datetime.fromtimestamp(self.inicio):%Y-%m-%d %H:%M} · duración {(time.time() - self.inicio) / 60:.1f} min",
               f"- Commits: {json.dumps(self.lab.estado.get('commits') or {}, ensure_ascii=False)}",
               f"- Resultado: {cuenta[PASA]} PASA · {cuenta[FALLA]} FALLA · {cuenta[PENDIENTE]} PENDIENTE · {cuenta[OMITIDA]} OMITIDA",
               "- PENDIENTE = falta un requisito físico o un destino autorizado: **no cuenta como aprobado**.",
               "- Evidencia: FÍSICA (Windows real, enlaces reales) · LOCAL (en el equipo, sin cortar la red) · SIMULADA.", "",
               "| Paso | Prueba | Estado | Evidencia | s | Detalle |", "|---|---|---|---|---|---|", *filas, "",
               "## Matriz F1–F12", "", "| Punto | Estado | Detalle | Fuente |", "|---|---|---|---|", *mfilas, ""]
        if self.metricas.get("estabilidad"):
            e = self.metricas["estabilidad"]
            lin += ["## Estabilidad (red simulada)", "", e["texto"], "",
                    "| Proceso | RAM ini/media/máx/fin MB | MB/h | Fuga | CPU % media/máx | Procesos | Hilos ini/máx/fin | Sockets ini/máx/fin |",
                    "|---|---|---|---|---|---|---|---|"]
            for n, v in e["procesos"].items():
                lin.append(f"| {n} | {v['ram_mb_inicio']} / {v.get('ram_mb_media')} / {v['ram_mb_max']} / {v['ram_mb_fin']} | "
                           f"{v['pendiente_mb_h']} | {v['fuga']} | {v['cpu_pct_medio']} / {v.get('cpu_pct_max_intervalo')} | "
                           f"{v.get('procesos_max')} | {v.get('hilos_inicio')} / {v.get('hilos_max')} / {v.get('hilos_fin')} | "
                           f"{v.get('sockets_inicio')} / {v.get('sockets_max')} / {v.get('sockets_fin')} |")
            lin += ["", f"- Latencia media {e['latencia_ms']['media']} ms (máx {e['latencia_ms']['max']}).",
                    f"- Cortes simulados: {len(e['cortes'])}; detección {e.get('deteccion_s')} s; recuperación {e.get('recuperacion_s')} s.",
                    f"- Disponibilidad de los motores (% de muestras con respuesta): {e.get('disponibilidad_motores_pct')}.",
                    f"- Sesión de supervisión (sin navegador), fases observadas: {e.get('sesion_fases')}.",
                    f"- Reinicios inesperados: {e.get('reinicios_inesperados') or 'ninguno'}; auditoría íntegra al final: "
                    f"{e.get('auditoria_integra_al_final')}.",
                    f"- Registros KB/h: {e['registros_kb_por_hora']}; errores: {e['errores']}", ""]
        if self.intervenciones:
            lin += ["## Lo que falta hacer en persona", ""] + [f"- {x}" for x in dict.fromkeys(self.intervenciones)] + [""]
        f = self.dir / "INFORME_VALIDACION_INTEGRAL.md"
        f.write_text("\n".join(lin), "utf-8")
        (self.dir / "validacion_integral.json").write_text(json.dumps(
            {"perfil": self.perfil, "pasos": self.pasos, "matriz": self.matriz, "metricas": self.metricas,
             "intervenciones": list(dict.fromkeys(self.intervenciones))}, ensure_ascii=False, indent=2, default=str), "utf-8")
        return f

    def ejecutar(self) -> int:
        L.decir(f"Validación integral · perfil {self.perfil} ({self.cfg['desc']}) · carpeta {self.lab.d}")
        pasos = [("01", "Versiones y dependencias", self.p01_versiones),
                 ("02", "Puertos", self.p02_puertos),
                 ("03", "Instalación aislada", self.p03_aislamiento),
                 ("04", "Inicio de los tres motores", self.p04_inicio),
                 ("05", "Interfaces físicas", self.p05_interfaces),
                 ("06", "Teléfono por USB", self.p06_telefono),
                 ("06b", "Teléfono: desconexión y reconexión en caliente", self.p06b_caliente),
                 ("07", "Diagnóstico de conectividad", self.p07_diagnostico),
                 ("08a", "Interrupción y recuperación (simulada)", self.p08a_interrupcion_simulada),
                 ("08b", "Guion F1–F12 (simulado)", self.p08b_guion_simulado),
                 ("08c", "Corte físico guiado (F1–F12 real)", self.p08c_fisica),
                 ("09", "Navegador de Forms en el portal de laboratorio", self.p09_navegador),
                 ("09b", "Sesión de navegador de laboratorio", self.p09b_sesiones),
                 ("10", "Reinicio individual de los motores", self.p10_reinicios),
                 ("11", "Consistencia de estados Network Engine / Monitor", self.p11_consistencia),
                 ("12", "Auditoría íntegra", self.p12_auditoria),
                 ("13", "Seguridad (credenciales, 403, 429, permisos, registros)", self.p13_seguridad),
                 ("14", "Estabilidad y recursos", self.p14_estabilidad),
                 ("15", "Cierre seguro del laboratorio", self.p15_cierre),
                 ("16", "Informes sin datos sensibles", self.p16_datos_sensibles)]
        for pid, titulo, fn in pasos:
            if pid in ("07", "08a", "08b", "09", "09b", "10", "11", "13", "14") and L.puerto_libre(self.lab.p["network_engine"]) \
                    and not any(p["id"] == "04" and p["estado"] == PASA for p in self.pasos):
                self.pasos.append({"id": pid, "titulo": titulo, "estado": OMITIDA, "evidencia": LOCAL, "s": 0,
                                   "detalle": "los motores no arrancaron (paso 04)"})
                continue
            self.paso(pid, titulo, fn)
        # F11 y F12 locales: los tres motores a la vez y rutas sin tocar (si no hubo guion físico)
        if any(p["id"] == "04" and p["estado"] == PASA for p in self.pasos):
            self.mf("F11", "Aprobado físicamente" if L.WINDOWS else "Aprobado en simulación",
                    "los tres motores respondieron a la vez en los puertos del laboratorio", "laboratorio")
        f = self.escribir()
        L.decir(f"\nInforme: {f}")
        return 1 if any(p["estado"] == FALLA for p in self.pasos) else 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Validación integral del laboratorio de Cauce")
    ap.add_argument("--dir", default=str(L.carpeta_por_omision()))
    ap.add_argument("--perfil", choices=list(PERFILES), default="rapido")
    ap.add_argument("--objetivo", default=OBJETIVO_POR_OMISION,
                    help="destino host:puerto de la prueba física (solo conexión TCP); por omisión github.com:443")
    ap.add_argument("--no-interactivo", action="store_true", help="no preguntar nada: lo físico queda PENDIENTE")
    ap.add_argument("--duracion", type=float, help="segundos de la prueba de estabilidad (sustituye a la del perfil)")
    a = ap.parse_args(argv)
    lab = L.Lab(Path(a.dir))
    L.validar_carpeta(lab.d, crear=False)
    interactivo = not a.no_interactivo and os.environ.get("CAUCE_LAB_SIN_PAUSA") != "1" and sys.stdin.isatty()
    return Integral(lab, a.perfil, interactivo, a.objetivo, a.duracion).ejecutar()


if __name__ == "__main__":
    raise SystemExit(main())
