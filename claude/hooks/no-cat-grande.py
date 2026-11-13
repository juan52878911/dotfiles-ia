#!/usr/bin/env python3
"""PreToolUse(Bash): bloquea volcar ENTERO un fichero grande con cat/less/more/head -n N/sed -n 'a,bp'
a la salida, porque luego se queda en el contexto y se relee en cada turno. Sugiere leer el trozo.
Grande es por LÍNEAS (MAX_LINEAS) o por BYTES (MAX_BYTES): un JSON minificado de 5 MB en una sola
línea también lo es. No toca: heredocs (cat <<EOF), redirecciones de la salida estándar a fichero
(cat x > y; ojo, 2>&1 y 2>/dev/null NO lo son), tuberías que acaban en un filtro que recorta
(cat x | grep …, | head -5, | wc -l), ficheros pequeños ni ficheros que no existen.
Si no puede medir un fichero que se va a volcar, BLOQUEA: no sabe no es lo mismo que no pasa nada.
Si no puede entender la orden, la deja pasar pero avisa por la salida de error.
Desactivar: NO_CAT_GRANDE=0."""
import glob, json, os, re, shlex, sys

INF = float("inf")
VOLCADORES = ("cat", "tac", "less", "more", "head", "tail", "sed", "nl", "bat",
              "xxd", "od", "strings", "base64", "pr", "fold")
LIMITADORES = ("wc", "grep", "egrep", "fgrep", "rg", "jq")
PREFIJOS = ("command", "time", "env", "sudo", "nohup", "nice", "stdbuf", "builtin", "exec")


def aviso(texto):
    print(f"no-cat-grande: {texto}", file=sys.stderr)


def entero(nombre, defecto):
    bruto = os.environ.get(nombre)
    if bruto is None:
        return defecto
    try:
        return int(bruto)
    except ValueError:
        aviso(f"{nombre}={bruto!r} no es un número; sigo con {defecto}")
        return defecto


MAX_LINEAS = entero("NO_CAT_GRANDE_LINEAS", 250)
MAX_BYTES = entero("NO_CAT_GRANDE_BYTES", 65536)


def mascara(texto):
    """El texto con lo que va entre comillas (y lo escapado) sustituido por espacios, para
    buscar separadores y redirecciones sin confundirlos con el contenido de un argumento."""
    salida, comilla, i = [], None, 0
    while i < len(texto):
        c = texto[i]
        if comilla is None and c == "\\" and i + 1 < len(texto):
            salida.append("  ")
            i += 2
            continue
        if comilla is None and c in "'\"":
            comilla = c
            salida.append(" ")
        elif comilla is not None and c == comilla:
            comilla = None
            salida.append(" ")
        elif comilla is not None:
            salida.append(" ")
        else:
            salida.append(c)
        i += 1
    return "".join(salida)


def partir(texto, patron):
    """Parte el texto por los separadores que casen FUERA de comillas."""
    m = mascara(texto)
    trozos, ini = [], 0
    for caso in re.finditer(patron, m):
        trozos.append(texto[ini:caso.start()])
        ini = caso.end()
    trozos.append(texto[ini:])
    return trozos


def saca_stdout(texto):
    """¿La salida estándar del tramo acaba en un fichero? '2>' y '2>&1' no la sacan."""
    return bool(re.search(r"(?<![0-9&])>|&>|1>", mascara(texto)))


def palabras(texto):
    texto = texto.strip()
    while texto[:1] in ("(", "{"):            # (cat f) y { cat f; } vuelcan igual
        texto = texto[1:].strip()
    while texto[-1:] in (")", "}", ";"):
        texto = texto[:-1].strip()
    try:
        w = shlex.split(texto)
    except ValueError:
        w = texto.split()
    while w and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w[0]) or w[0].lstrip("\\") in PREFIJOS):
        w = w[1:]
    if w:
        w[0] = os.path.basename(w[0].lstrip("\\"))
    return w


def tope(w):
    """Cota superior de líneas que el comando puede escribir. INF = sin cota."""
    c = w[0]
    if c in ("head", "tail"):
        n, i = 10, 1
        while i < len(w):
            x = w[i]
            m = re.fullmatch(r"(?:-n|--lines=)(\+?\d+)", x) or re.fullmatch(r"-(\d+)", x)
            if m:
                n = INF if m.group(1).startswith("+") else int(m.group(1))
            elif x in ("-n", "--lines") and i + 1 < len(w):
                i += 1
                v = w[i]
                n = int(v) if v.isdigit() else INF
            elif re.fullmatch(r"(?:-c|--bytes=)(\d+)", x) or x in ("-c", "--bytes"):
                m = re.fullmatch(r"(?:-c|--bytes=)(\d+)", x)
                v = m.group(1) if m else (w[i + 1] if i + 1 < len(w) else "0")
                n = 0 if v.isdigit() and int(v) <= MAX_BYTES else INF
            i += 1
        return n
    if c == "sed":
        if not any(re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", x) for x in w[1:]):
            return INF                      # sin -n imprime el fichero entero
        cota = INF
        for x in w[1:]:
            m = re.fullmatch(r"(\d+),(\d+)p", x.strip("'\""))
            if m:
                cota = min(cota, int(m.group(2)) - int(m.group(1)) + 1)
        return cota                         # '1,$p' o 'p' a secas no acotan
    return INF


def limita(etapa):
    """¿Esta etapa recorta a algo pequeño lo que llega al contexto?"""
    w = palabras(etapa)
    if not w:
        return False
    if w[0] in LIMITADORES:
        return True
    return w[0] in VOLCADORES and tope(w) <= MAX_LINEAS


def resolver(arg, cwd):
    """Los ficheros existentes a los que se refiere el argumento (expande ~, $VAR y comodines)."""
    a = arg.replace("${PWD}", cwd).replace("$PWD", cwd)   # el PWD que cuenta es el del evento
    a = os.path.expanduser(os.path.expandvars(a))
    if not os.path.isabs(a):
        a = os.path.join(cwd, a)
    if any(ch in a for ch in "*?["):
        return sorted(p for p in glob.glob(a) if os.path.isfile(p))
    return [a] if os.path.isfile(a) else []


def medir(p):
    """(líneas, bytes) del fichero. Lanza OSError si no se puede mirar."""
    tam = os.path.getsize(p)
    if tam > MAX_BYTES:
        return None, tam                    # ya es grande: no hace falta contarlo
    with open(p, "rb") as f:
        return sum(1 for _ in f), tam


def consejo(f):
    return (f"Lee solo lo que necesitas: `grep -n <patrón> {f}` para ubicar y luego Read con "
            f"offset/limit (o `sed -n 'A,Bp' {f}` con un rango de ≤{MAX_LINEAS} líneas). Si de "
            f"verdad necesitas el fichero completo para entenderlo, delega la lectura en un "
            f"subagente y quédate con su resumen.")


def revisar(etapa, cwd):
    """El motivo por el que esta etapa no debe correr, o None."""
    w = palabras(etapa)
    if not w or w[0] not in VOLCADORES or tope(w) <= MAX_LINEAS:
        return None
    for arg in w[1:]:
        if arg.startswith("-"):
            continue
        for p in resolver(arg, cwd):
            nombre = os.path.relpath(p, cwd) if p.startswith(cwd) else p
            try:
                n, tam = medir(p)
            except OSError as e:
                return (f"No he podido medir {nombre} ({e.strerror}), así que no sé si volcarlo "
                        f"llenaría el contexto. Bloqueo por si acaso. " + consejo(nombre))
            if tam > MAX_BYTES:
                return (f"{nombre} ocupa {tam} bytes: volcarlo entero lo deja en el contexto y se "
                        f"relee en cada turno. " + consejo(nombre))
            if n is not None and n > MAX_LINEAS:
                return (f"{nombre} tiene {n} líneas: volcarlo entero lo deja en el contexto y se "
                        f"relee en cada turno. " + consejo(nombre))
    return None


def main():
    if os.environ.get("NO_CAT_GRANDE") == "0":
        return
    try:
        ev = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        aviso(f"la entrada no es JSON ({e}); dejo pasar la orden sin mirarla")
        return
    entrada = ev.get("tool_input")
    if not isinstance(entrada, dict):
        aviso(f"tool_input es {type(entrada).__name__}, no un objeto; dejo pasar la orden sin mirarla")
        return
    cmd = entrada.get("command")
    if not isinstance(cmd, str):
        aviso("tool_input.command no es una cadena; dejo pasar la orden sin mirarla")
        return
    bruto = ev.get("cwd")
    if isinstance(bruto, str) and os.path.isdir(bruto):
        cwd = bruto
    else:
        cwd = os.getcwd()
        aviso(f"cwd no sirve ({bruto!r}); resuelvo las rutas relativas contra {cwd}")

    tramos = partir(cmd, r"&&|\|\||;|\n|&(?!&)")
    for sust in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", mascara(cmd)):
        tramos.extend(x for x in sust if x)          # lo que corre dentro de $( ) o ` ` también vuelca

    for tramo in tramos:
        if not tramo.strip():
            continue
        w = palabras(tramo)
        if w and w[0] == "cd":                        # el cwd de los tramos siguientes cambia
            destino = w[1] if len(w) > 1 else "~"
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(os.path.expandvars(destino))))
            continue
        if "<<" in mascara(tramo):                    # heredoc: no lee un fichero
            continue
        etapas = partir(tramo, r"\|(?!\|)")
        if saca_stdout(etapas[-1]):                   # la salida acaba en un fichero, no en el contexto
            continue
        if len(etapas) > 1 and limita(etapas[-1]):    # la última etapa recorta lo que llega
            continue
        for etapa in etapas:
            motivo = revisar(etapa, cwd)
            if motivo:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": motivo}}, ensure_ascii=False))
                return


try:
    main()
except Exception as e:
    aviso(f"no he podido analizar la orden ({e.__class__.__name__}: {e}); la dejo pasar")
