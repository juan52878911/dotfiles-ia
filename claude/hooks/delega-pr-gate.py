#!/usr/bin/env python3
"""PreToolUse(Bash): bloquea `gh pr create` desde una rama de trabajador (delega/* u orq/*) si en el
audit.jsonl de delega no hay un `integrate` correcto (clean o verified) de esa rama en su sha actual.
Un PR abierto antes de integrar y verificar trajo 2 regresiones; este hook dice el comando exacto.

Las demás ramas pasan sin tocar nada. Si no se puede saber la rama, deja pasar.
Desactivar: DELEGA_PR_GATE=0. Prefijos: DELEGA_PR_GATE_RAMAS (por defecto "delega/,orq/")."""
import json, os, re, shlex, subprocess, sys

OK = ("clean", "verified")


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace", timeout=10)
    return r.stdout.strip() if r.returncode == 0 else ""


def state_dir():
    if os.environ.get("DELEGA_STATE_DIR"):
        return os.path.expanduser(os.environ["DELEGA_STATE_DIR"])
    try:
        import tomllib
        with open(os.path.expanduser(os.environ.get("DELEGA_CONFIG", "~/.config/delega/config.toml")), "rb") as f:
            sd = (tomllib.load(f).get("general") or {}).get("state_dir")
        if sd:
            return os.path.expanduser(sd)
    except Exception:
        pass
    return os.path.expanduser("~/.local/state/delega")


def integrado(rama, sha):
    """Último integrate de la rama: (resultado, sha) o None."""
    ultimo = None
    try:
        with open(os.path.join(state_dir(), "audit.jsonl"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"integrate"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("event") == "integrate" and e.get("branch") == rama:
                    ultimo = (e.get("result"), e.get("sha") or "")
    except OSError:
        pass
    return ultimo


def opcion(palabras, *nombres):
    for i, w in enumerate(palabras):
        for n in nombres:
            if w == n and i + 1 < len(palabras):
                return palabras[i + 1]
            if w.startswith(n + "="):
                return w.split("=", 1)[1]
    return None


def main():
    if os.environ.get("DELEGA_PR_GATE") == "0":
        return 0
    ev = json.load(sys.stdin)
    cmd = (ev.get("tool_input") or {}).get("command", "")
    if not re.search(r"\bgh\s+pr\s+create\b", cmd):
        return 0
    prefijos = tuple(p for p in os.environ.get("DELEGA_PR_GATE_RAMAS", "delega/,orq/").split(",") if p)
    cwd = ev.get("cwd") or os.getcwd()
    for tramo in re.split(r"&&|\|\||;|\n", cmd):
        t = tramo.strip()
        m = re.match(r"cd\s+(\S+)$", t)
        if m:
            cwd = os.path.join(cwd, os.path.expanduser(m.group(1).strip("'\"")))
            continue
        if not re.search(r"\bgh\s+pr\s+create\b", t):
            continue
        try:
            palabras = shlex.split(t)
        except ValueError:
            palabras = t.split()
        rama = opcion(palabras, "--head", "-H") or git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        rama = rama.split(":", 1)[-1]          # owner:rama
        if not rama.startswith(prefijos):
            continue
        base = opcion(palabras, "--base", "-B")
        sha = git(cwd, "rev-parse", "--short", rama)
        ultimo = integrado(rama, sha)
        repo = git(cwd, "rev-parse", "--show-toplevel") or cwd
        orden = f"delega integrate {shlex.quote(repo)} {shlex.quote(rama)}" + (f" --into {shlex.quote(base)}" if base else "")
        if ultimo is None:
            motivo = f"no hay ningún `integrate` de {rama} en el audit de delega"
        elif ultimo[0] not in OK:
            motivo = f"el último `integrate` de {rama} terminó en «{ultimo[0]}»"
        elif sha and ultimo[1] and not (sha.startswith(ultimo[1]) or ultimo[1].startswith(sha)):
            motivo = f"el último `integrate` correcto de {rama} fue en {ultimo[1]} y la rama está ahora en {sha}"
        else:
            continue
        print(f"delega-pr-gate bloqueó `gh pr create`: {motivo}. Antes del PR, integra y verifica:\n  {orden}\n"
              "y abre el PR solo si termina bien (merge sin conflictos y verify del proyecto en verde).", file=sys.stderr)
        return 2
    return 0


try:
    sys.exit(main())
except SystemExit:
    raise
except Exception:
    sys.exit(0)
