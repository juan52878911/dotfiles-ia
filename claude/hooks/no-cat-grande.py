#!/usr/bin/env python3
"""PreToolUse(Bash): bloquea volcar ENTERO un fichero grande con cat/less/more/head -n N/sed -n 'a,bp'
a la salida, porque luego se queda en el contexto y se relee en cada turno. Sugiere leer el trozo.
No toca: heredocs (cat <<EOF), redirecciones (cat x > y), tuberías a filtros (cat x | grep …),
ficheros pequeños (< MAX_LINEAS) ni ficheros que no existen. Desactivar: NO_CAT_GRANDE=0."""
import json, os, re, shlex, sys

MAX_LINEAS = int(os.environ.get("NO_CAT_GRANDE_LINEAS", "250"))

def lineas(p):
    try:
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

def main():
    if os.environ.get("NO_CAT_GRANDE") == "0":
        return
    ev = json.load(sys.stdin)
    cmd = (ev.get("tool_input") or {}).get("command", "")
    cwd = ev.get("cwd") or os.getcwd()
    # Cada tramo de una cadena con && ; || se mira por separado; un tramo con tubería o
    # redirección no vuelca a la salida (o la filtra), así que se deja pasar.
    for tramo in re.split(r"&&|\|\||;|\n", cmd):
        t = tramo.strip()
        if not t or "<<" in t or ">" in t or "|" in t:
            continue
        try:
            w = shlex.split(t)
        except ValueError:
            continue
        if not w or w[0] not in ("cat", "less", "more", "head", "tail", "sed", "nl", "bat"):
            continue
        if w[0] in ("head", "tail"):
            n = 10
            for i, x in enumerate(w):
                m = re.fullmatch(r"-n?(\d+)", x) or (re.fullmatch(r"\d+", x) and w[i - 1] in ("-n",) and re.fullmatch(r"(\d+)", x))
                if m:
                    n = int(m.group(1))
            if n <= MAX_LINEAS:
                continue
        if w[0] == "sed":
            rng = next((re.fullmatch(r"'?(\d+),(\d+)p'?", x) for x in w if re.fullmatch(r"'?\d+,\d+p'?", x)), None)
            if not rng or int(rng.group(2)) - int(rng.group(1)) <= MAX_LINEAS:
                continue
        ficheros = [x for x in w[1:] if not x.startswith("-") and os.path.isfile(os.path.join(cwd, x))]
        grandes = [(f, lineas(os.path.join(cwd, f))) for f in ficheros]
        grandes = [(f, n) for f, n in grandes if n > MAX_LINEAS]
        if grandes:
            f, n = grandes[0]
            motivo = (f"{f} tiene {n} líneas: volcarlo entero lo deja en el contexto y se relee en cada turno. "
                      f"Lee solo lo que necesitas: `grep -n <patrón> {f}` para ubicar y luego Read con offset/limit "
                      f"(o `sed -n 'A,Bp' {f}` con un rango de ≤{MAX_LINEAS} líneas). Si de verdad necesitas el fichero "
                      f"completo para entenderlo, delega la lectura en un subagente y quédate con su resumen.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": motivo}}, ensure_ascii=False))
            return

try:
    main()
except Exception:
    pass
