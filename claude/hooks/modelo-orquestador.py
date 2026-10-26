#!/usr/bin/env python3
"""SessionStart: avisa si una sesión orquestadora arranca con un modelo de contexto 1M (`[1m]`).
Un orquestador con 650k de contexto se agotó tres veces y releía todo en cada turno: el orquestador
va en Opus sin [1m] (o Sonnet si el encargo tiene menos de 4 briefs) y con contexto < 300k.

Es orquestadora la sesión cuya carpeta encaja con el repo_match (git_remote) de algún perfil de delega,
o la que tiene DELEGA_OWNER. Los trabajadores de delega (DELEGA_CHANNEL) no avisan. Solo avisa, no bloquea.
Desactivar: MODELO_ORQUESTADOR=0."""
import json, os, re, subprocess, sys


def modelo(ev):
    m = ev.get("model")
    if isinstance(m, dict):
        m = m.get("id") or m.get("display_name")
    if m:
        return str(m)
    if os.environ.get("ANTHROPIC_MODEL"):
        return os.environ["ANTHROPIC_MODEL"]
    try:
        cfg = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
        with open(os.path.join(cfg, "settings.json")) as f:
            return str(json.load(f).get("model") or "")
    except Exception:
        return ""


def orquestadora(cwd):
    if os.environ.get("DELEGA_OWNER"):
        return True
    try:
        import tomllib
        with open(os.path.expanduser(os.environ.get("DELEGA_CONFIG", "~/.config/delega/config.toml")), "rb") as f:
            perfiles = tomllib.load(f).get("profiles") or {}
    except Exception:
        return False
    patrones = [(p.get("repo_match") or {}).get("git_remote") for p in perfiles.values()]
    patrones = [x for x in patrones if x]
    if not patrones:
        return False
    r = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5)
    url = r.stdout.strip()
    return bool(url) and any(re.search(p, url) for p in patrones)


def main():
    if os.environ.get("MODELO_ORQUESTADOR") == "0" or os.environ.get("DELEGA_CHANNEL"):
        return
    ev = json.load(sys.stdin)
    m = modelo(ev)
    if "[1m]" not in m.lower():
        return
    if not orquestadora(ev.get("cwd") or os.getcwd()):
        return
    aviso = (f"Esta sesión orquesta trabajo de delega con el modelo {m} (contexto 1M). El orquestador va en Opus "
             "sin [1m] (o Sonnet si hay menos de 4 briefs) y con contexto < 300k: cambia el modelo con /model "
             "antes de seguir, o delega la lectura larga en subagentes.")
    print(json.dumps({"systemMessage": aviso,
                      "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": aviso}},
                     ensure_ascii=False))


try:
    main()
except Exception:
    pass
