#!/usr/bin/env python3
"""PreToolUse(Bash): antes de leer la historia de un fichero con git (git blame, git log -L,
git log -p <ruta>, git log -- <ruta>), propone `chrono explain <ruta>[:A-B]`, que da los commits
que escribieron esas líneas, si eran arreglos, tickets/PRs y autores en ~500 tokens frente a los
~10 000 de la salida cruda.
Solo actúa si hay binario de chrono y un índice (.chrono/index.db) en el directorio del evento o en
uno superior; si no, no dice nada. Corta la orden UNA vez por sesión y fichero: si el agente la repite
tal cual, pasa (a veces hace falta el parche literal). `git log --oneline -1 …` y los rangos de ramas
no los toca: son baratos y chrono no los sustituye.
Desactivar: CHRONO_PRIMERO=0."""
import hashlib, json, os, re, shlex, shutil, sys, time

ESTADO = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "chrono-primero")


def aviso(texto):
    print(f"chrono-primero: {texto}", file=sys.stderr)


def binario():
    for c in (shutil.which("chrono"), os.path.expanduser("~/.local/bin/chrono"),
              os.path.expanduser("~/.cargo/bin/chrono")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def indice(cwd):
    d = os.path.abspath(cwd)
    while True:
        if os.path.isfile(os.path.join(d, ".chrono", "index.db")):
            return d
        padre = os.path.dirname(d)
        if padre == d:
            return None
        d = padre


def tramos(cmd):
    """Las órdenes simples de la línea, sin lo que va entre comillas para partir."""
    trozos = re.split(r"&&|\|\||[;|\n]|\$\(|`|\)", cmd)
    return [t.strip() for t in trozos if t.strip()]


def objetivo(tramo, cwd):
    """(fichero, rango o None, forma) si el tramo lee la historia de un fichero; si no, None."""
    try:
        w = shlex.split(tramo)
    except ValueError:
        w = tramo.split()
    while w and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w[0]) or w[0] in ("command", "env", "time")):
        w = w[1:]
    if not w or os.path.basename(w[0]) != "git":
        return None
    i = 1
    while i < len(w) and w[i] in ("-C", "-c", "--no-pager", "-P"):
        i += 2 if w[i] in ("-C", "-c") else 1
    if i >= len(w):
        return None
    sub, args = w[i], w[i + 1:]
    if sub == "blame":
        rango, rutas = None, []
        j = 0
        while j < len(args):
            a = args[j]
            m = re.fullmatch(r"-L(\d+),\+?(\d+)", a)
            if a == "-L" and j + 1 < len(args):
                m = re.fullmatch(r"(\d+),\+?(\d+)", args[j + 1])
                j += 1
            if m:
                rango = (m.group(1), m.group(2))
            elif not a.startswith("-") and a != "--":
                rutas.append(a)
            j += 1
        return (rutas[-1], rango, "git blame") if rutas else None
    if sub != "log":
        return None
    for j, a in enumerate(args):
        x = a[2:] if a.startswith("-L") and len(a) > 2 else (args[j + 1] if a == "-L" and j + 1 < len(args) else None)
        if x is not None:
            m = re.fullmatch(r"(\d+),(\d+):(.+)", x)
            if m:
                return (m.group(3), (m.group(1), m.group(2)), "git log -L")
            m = re.fullmatch(r":[^:]+:(.+)", x)
            if m:
                return (m.group(1), None, "git log -L")
    if any(re.fullmatch(r"-(1|n1)|--max-count=1", a) for a in args) or \
            any(a == "-n" and k + 1 < len(args) and args[k + 1] == "1" for k, a in enumerate(args)):
        return None                                   # «cuándo se tocó por última vez»: barato
    if "--" in args:
        rutas = [a for a in args[args.index("--") + 1:] if not a.startswith("-")]
        if rutas:
            return (rutas[0], None, "git log -- <ruta>")
    if any(a in ("-p", "--patch", "-u", "--follow") for a in args):
        rutas = [a for a in args if not a.startswith("-") and os.path.exists(os.path.join(cwd, a))]
        if rutas:
            return (rutas[0], None, "git log -p <ruta>")
    return None


def ya_avisado(sesion, clave):
    """True si ya se cortó esta clave en la sesión; si no, la apunta y devuelve False."""
    os.makedirs(ESTADO, exist_ok=True)
    ahora = time.time()
    for n in os.listdir(ESTADO):                      # limpia sesiones de hace más de dos días
        p = os.path.join(ESTADO, n)
        try:
            if ahora - os.path.getmtime(p) > 2 * 86400:
                os.remove(p)
        except OSError:
            pass
    nombre = hashlib.sha1(str(sesion).encode()).hexdigest()[:16]
    ruta = os.path.join(ESTADO, nombre)
    vistas = set()
    if os.path.exists(ruta):
        with open(ruta) as f:
            vistas = set(f.read().split())
    if clave in vistas:
        return True
    with open(ruta, "a") as f:
        f.write(clave + "\n")
    return False


def main():
    if os.environ.get("CHRONO_PRIMERO") == "0":
        return
    try:
        ev = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    cmd = (ev.get("tool_input") or {}).get("command")
    if not isinstance(cmd, str) or "chrono" in cmd:
        return
    cwd = ev.get("cwd") if isinstance(ev.get("cwd"), str) and os.path.isdir(ev["cwd"]) else os.getcwd()
    encontrado = None
    for t in tramos(cmd):
        encontrado = objetivo(t, cwd)
        if encontrado:
            break
    if not encontrado or not binario():
        return
    raiz = indice(cwd)
    if not raiz:
        return
    fichero, rango, forma = encontrado
    clave = hashlib.sha1(f"{fichero}|{cmd}".encode()).hexdigest()[:16]
    if ya_avisado(ev.get("session_id") or os.getppid(), clave):
        return
    destino = fichero + (f":{rango[0]}-{rango[1]}" if rango else "")
    motivo = (f"Este repo tiene índice de chrono ({raiz}/.chrono). En vez de `{forma}`, prueba primero "
              f"`chrono explain {destino}` (o la herramienta MCP `explain` con target={destino}): da los "
              f"commits que escribieron esas líneas, si eran arreglos, tickets/PRs y autores en ~500 tokens "
              f"frente a ~10 000. Para el parche literal de un commit concreto, `git show <sha>`. Si de "
              f"verdad necesitas esta orden, repítela tal cual y pasará.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": motivo}}, ensure_ascii=False))


try:
    main()
except Exception as e:
    aviso(f"no he podido analizar la orden ({e.__class__.__name__}: {e}); la dejo pasar")
