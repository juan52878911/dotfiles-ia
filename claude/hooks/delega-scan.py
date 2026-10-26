#!/usr/bin/env python3
"""PreToolUse(Bash): antes de que algo salga del Mac (git commit, git push, gh pr/issue/release/gist que
publican, gh api con cuerpo), pasa `delega scan` sobre lo que va a salir y bloquea si aparece un término de
forbidden_terms_file del perfil de delega que encaja con la carpeta. Nunca imprime el término, solo dónde.

Rápido: los comandos que no publican salen sin leer nada, y si ningún perfil tiene forbidden_terms_file se
sale tras leer la configuración. Si delega no está instalado o algo falla, deja pasar (no es la única
barrera: `delega run` ya escanea brief y rama). Desactivar: DELEGA_SCAN_HOOK=0."""
import json, os, re, shlex, shutil, subprocess, sys, tempfile

PUBLICA = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push)\b|"
                     r"\bgh\s+(?:pr\s+(?:create|edit|comment|review)|issue\s+(?:create|edit|comment)|"
                     r"release\s+(?:create|edit)|gist\s+(?:create|edit)|api)\b")
EXIT_HITS = 9


def config_path():
    return os.path.expanduser(os.environ.get("DELEGA_CONFIG", "~/.config/delega/config.toml"))


def hay_terminos():
    """¿Algún perfil tiene un forbidden_terms_file legible? (sin cargar delega)"""
    try:
        import tomllib
        with open(config_path(), "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return False
    for p in (data.get("profiles") or {}).values():
        ruta = p.get("forbidden_terms_file")
        if ruta and os.path.isfile(os.path.expanduser(ruta)):
            return True
    return False


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace", timeout=20)
    return r.stdout if r.returncode == 0 else ""


def carpeta(tramo, cwd):
    m = re.search(r"\bgit\s+-C\s+(\S+)", tramo)
    if m:
        return os.path.join(cwd, os.path.expanduser(m.group(1).strip("'\"")))
    return cwd


def ficheros_de_cuerpo(palabras, cwd):
    """Contenido de --body-file/-F/--notes-file y de los ficheros de `gh gist create`."""
    out = []
    for i, w in enumerate(palabras[:-1]):
        if w in ("-F", "--body-file", "--notes-file", "--file") and palabras[i + 1] != "-":
            out.append(os.path.join(cwd, os.path.expanduser(palabras[i + 1])))
    if palabras[:3] == ["gh", "gist", "create"]:
        out += [os.path.join(cwd, w) for w in palabras[3:] if not w.startswith("-")]
    textos = []
    for p in out:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                textos.append(f.read())
        except OSError:
            pass
    return textos


def escanear(delega, cwd, args):
    r = subprocess.run([delega, "scan", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr).strip()


def revisar(tramo, cwd, delega):
    """(motivo) si el tramo publica algo con términos prohibidos, si no None."""
    repo = carpeta(tramo, cwd)
    try:
        palabras = shlex.split(tramo)
    except ValueError:
        palabras = tramo.split()
    # Texto que sale: el propio comando (mensaje -m, --title, --body…) y lo que se va a commitear.
    textos = [tramo] + ficheros_de_cuerpo(palabras, cwd)
    if re.search(r"\bgit\b.*\bcommit\b", tramo):
        textos.append(git(repo, "diff", "--cached", "-U0", "--no-color"))
        if re.search(r"\s(?:-a|--all|-[a-zA-Z]*a[a-zA-Z]*)\b", tramo.split("commit", 1)[1]):
            textos.append(git(repo, "diff", "-U0", "--no-color"))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(textos))
        tmp = f.name
    try:
        rc, salida = escanear(delega, repo, ["--text", tmp])
    finally:
        os.unlink(tmp)
    if rc == EXIT_HITS:
        return "el comando, el mensaje o los cambios preparados contienen términos prohibidos del perfil (" + salida + ")"
    # Commits que salen: git push y gh pr create publican la rama entera.
    if re.search(r"\bgit\b.*\bpush\b|\bgh\s+pr\s+create\b", tramo):
        rama = "HEAD"
        m = re.search(r"\bpush\b(?:\s+-\S+)*\s+\S+\s+([^\s:-][^\s:]*)", tramo)
        if m and git(repo, "rev-parse", "--verify", "-q", m.group(1)).strip():
            rama = m.group(1)
        base = git(repo, "rev-parse", "-q", "--verify", rama + "@{upstream}").strip()
        rc, salida = escanear(delega, repo, [repo, rama] + ([base] if base else []))
        if rc == EXIT_HITS:
            return "los commits que salen contienen términos prohibidos del perfil:\n" + salida
    return None


def main():
    if os.environ.get("DELEGA_SCAN_HOOK") == "0":
        return 0
    ev = json.load(sys.stdin)
    cmd = (ev.get("tool_input") or {}).get("command", "")
    if not PUBLICA.search(cmd):
        return 0
    delega = shutil.which("delega")
    if not delega or not hay_terminos():
        return 0
    cwd = ev.get("cwd") or os.getcwd()
    for tramo in re.split(r"&&|\|\||;|\n", cmd):
        t = tramo.strip()
        m = re.match(r"cd\s+(\S+)$", t)
        if m:
            cwd = os.path.join(cwd, os.path.expanduser(m.group(1).strip("'\"")))
            continue
        if not PUBLICA.search(t):
            continue
        motivo = revisar(t, cwd, delega)
        if motivo:
            print(f"delega-scan bloqueó la publicación: {motivo}\nQuita esos términos (usa datos inventados) "
                  "y vuelve a intentarlo; `delega scan` dice dónde están sin mostrarlos.", file=sys.stderr)
            return 2
    return 0


try:
    sys.exit(main())
except SystemExit:
    raise
except Exception:
    sys.exit(0)
