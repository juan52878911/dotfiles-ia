#!/usr/bin/env python3
"""Mide delega.lint contra una verdad congelada de huecos de brief. No versiona nada de los briefs.

Uso: medir_lint.py VERDAD.json --estado DIR [--patron REGEX] [--env-file RUTA] [--limpios]

VERDAD.json: {"briefs": [{"id", "job", "tipo": "hueco"|"control", "huecos": [{"g", ...}]}]}.
Los briefs se leen de <estado>/<job>/brief.md (el directorio de estado del orquestador que los lanzó).
La correspondencia entre regla y hueco es MAPA, escrito a mano antes de medir: un hueco cuenta
como detectado si su brief recibe un aviso de la regla que le corresponde, no un aviso cualquiera.
Imprime solo identificadores, códigos de regla y cifras, nunca texto de los briefs."""
import argparse
import collections
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from delega import lint  # noqa: E402

# hueco → reglas que lo cubren (fijado antes de ejecutar; las de la verdad que ninguna regla
# de texto puede ver quedan con lista vacía)
MAPA = {
    "B01-G1": ["live"], "B02-G1": ["branch"], "B03-G1": [], "B04-G1": ["script"], "B05-G1": ["deadline"],
    "B06-G1": ["live"], "B06-G2": [], "B07-G1": ["live"], "B08-G1": ["ref"], "B08-G2": ["script"],
    "B09-G1": ["branch"], "B10-G1": [], "B10-G2": [],
}


def senal(d):
    """¿El trabajo tuvo preguntas, bloqueos o mensajes del orquestador? (entonces no es limpio)"""
    for f in ("notices.jsonl", "avisos.jsonl"):
        p = os.path.join(d, f)
        if os.path.exists(p):
            for ln in open(p, errors="replace"):
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                if (j.get("kind") or j.get("tipo")) in ("question", "pregunta", "blocker", "bloqueo"):
                    return True
    for f in ("inbox.jsonl", "buzon.jsonl"):
        p = os.path.join(d, f)
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return True
    for f in ("progress.log", "progreso.log"):
        p = os.path.join(d, f)
        if os.path.exists(p) and "MESSAGE" in open(p, errors="ignore").read():
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("verdad")
    ap.add_argument("--estado", required=True, help="directorio con <job>/brief.md")
    ap.add_argument("--patron", default=r"^(feature|fix|chore)/", help="branch_pattern del perfil")
    ap.add_argument("--env-file", default=None, help="env_file del perfil (por defecto ninguno)")
    ap.add_argument("--limpios", action="store_true", help="mide también el ruido en briefs sin preguntas")
    a = ap.parse_args()
    estado = os.path.expanduser(a.estado)
    v = json.load(open(a.verdad))
    run = lambda txt: lint.check(txt, pattern=a.patron, env_file=a.env_file)

    total, hits, t0, n = 0, [], time.perf_counter(), 0
    controles = {}
    for b in v["briefs"]:
        txt = open(os.path.join(estado, b["job"], "brief.md"), encoding="utf-8", errors="replace").read()
        found = run(txt)
        n += 1
        codes = sorted({c for _, c, _ in found})
        if b["tipo"] == "control":
            controles[b["id"]] = codes
            print(f"{b['id']} control  avisos={codes}")
            continue
        det = []
        for h in b["huecos"]:
            total += 1
            if set(MAPA.get(h["g"], [])) & set(codes):
                det.append(h["g"])
        hits += det
        inutiles = [c for c in codes if not any(c in MAPA.get(h["g"], []) for h in b["huecos"])]
        print(f"{b['id']} hueco    avisos={codes}  detectados={det}  sin_hueco={inutiles}")
    ms = (time.perf_counter() - t0) / n * 1000
    peor = max((len(c) for c in controles.values()), default=0)
    print(f"\nrecall: {len(hits)}/{total}  avisos por control: "
          f"{', '.join(f'{k}={len(c)}' for k, c in controles.items())} (máx {peor})  ms/brief={ms:.2f}")

    if a.limpios:
        dev = {b["job"] for b in v["briefs"]}
        ids = [d for d in sorted(os.listdir(estado)) if d not in dev
               and os.path.exists(os.path.join(estado, d, "brief.md")) and not senal(os.path.join(estado, d))]
        tasa, con = collections.Counter(), 0
        for d in ids:
            codes = {c for _, c, _ in run(open(os.path.join(estado, d, "brief.md"), errors="replace").read())}
            tasa.update(codes)
            con += bool(codes)
        print(f"limpios: {len(ids)} briefs; con algún aviso {con} ({con / max(1, len(ids)):.0%}); por regla: "
              + ", ".join(f"{k}={c} ({c / max(1, len(ids)):.0%})" for k, c in tasa.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
