#!/usr/bin/env python3
"""Declara un marketplace local de Claude Code y activa su plugin en uno o varios directorios de
configuración (~/.claude y los CLAUDE_CONFIG_DIR de otras cuentas), sin pisar nada más.
Después, si encuentra `claude`, lo instala o lo actualiza en cada uno (best effort).
Uso: activar-plugin.py <marketplace> <plugin> <ruta del marketplace> <config_dir>... [--claude BIN] [--quitar plugin@mercado]...
--quitar retira un plugin anterior (p. ej. tras un cambio de nombre): lo desactiva, quita su marketplace
de los settings y, con `claude`, lo desinstala. Si no está, no hace nada."""
import json, os, shutil, subprocess, sys

args = sys.argv[1:]
cli = None
if "--claude" in args:
    i = args.index("--claude")
    cli = args[i + 1]
    del args[i:i + 2]
quitar = []
while "--quitar" in args:
    i = args.index("--quitar")
    quitar.append(args[i + 1])
    del args[i:i + 2]
mercado, plugin, ruta, *dirs = args
clave = f"{plugin}@{mercado}"


def fusionar(d):
    p = os.path.join(d, "settings.json")
    try:
        with open(p) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {}
    antes = json.dumps(s, sort_keys=True)
    s.setdefault("extraKnownMarketplaces", {})[mercado] = {"source": {"source": "directory", "path": ruta}}
    s.setdefault("enabledPlugins", {})[clave] = True
    if json.dumps(s, sort_keys=True) == antes:
        return
    os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp-dotfiles-ia"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)
    print(f"{clave}: activado en {p}")


def instalar(d):
    env = dict(os.environ)
    if os.path.realpath(d) != os.path.realpath(os.path.expanduser("~/.claude")):
        env["CLAUDE_CONFIG_DIR"] = d
    pasos = [["plugin", "marketplace", "add", ruta], ["plugin", "marketplace", "update", mercado], ["plugin", "install", clave], ["plugin", "update", clave]]
    for paso in pasos:
        try:
            subprocess.run([cli, *paso], env=env, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"{clave}: {' '.join(paso)} falló en {d} ({type(e).__name__})")
            return


def retirar(d, viejo):
    p = os.path.join(d, "settings.json")
    try:
        with open(p) as f:
            s = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    merc_viejo = viejo.split("@", 1)[1]
    estaba = viejo in s.get("enabledPlugins", {}) or merc_viejo in s.get("extraKnownMarketplaces", {})
    if not estaba:
        return
    s.get("enabledPlugins", {}).pop(viejo, None)
    s.get("extraKnownMarketplaces", {}).pop(merc_viejo, None)
    tmp = p + ".tmp-dotfiles-ia"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)
    print(f"{viejo}: retirado de {p}")
    if cli:
        env = dict(os.environ)
        if os.path.realpath(d) != os.path.realpath(os.path.expanduser("~/.claude")):
            env["CLAUDE_CONFIG_DIR"] = d
        for paso in (["plugin", "uninstall", viejo], ["plugin", "marketplace", "remove", merc_viejo]):
            try:
                subprocess.run([cli, *paso], env=env, capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                pass


cli = cli or shutil.which("claude")
for d in dirs:
    d = os.path.expanduser(d)
    for viejo in quitar:
        retirar(d, viejo)
    fusionar(d)
    if cli:
        instalar(d)
