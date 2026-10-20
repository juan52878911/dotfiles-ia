#!/usr/bin/env python3
"""Fusiona settings-base.json en ~/.claude/settings.json (y limpia settings.local.json) sin pisar nada más.
Idempotente: añade las reglas deny y los hooks que falten, quita de allow los comodines peligrosos y solo
escribe si algo cambia (con copia .bak-dotfiles-ia la primera vez que toca cada fichero).
Uso: fusionar-settings.py [--seco]"""
import json, os, shutil, sys

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings-base.json")
CLAUDE = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
seco = "--seco" in sys.argv


def cargar(p):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def guardar(p, d, antes):
    if d == antes:
        return False
    if seco:
        print(f"cambiaría {p}")
        return True
    if os.path.exists(p) and not os.path.exists(p + ".bak-dotfiles-ia"):
        shutil.copy2(p, p + ".bak-dotfiles-ia")
    tmp = p + ".tmp-dotfiles-ia"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)
    print(f"actualizado {p}")
    return True


base = cargar(BASE)
quitar = set(base["permisos"].get("quitar_de_allow", []))

p = os.path.join(CLAUDE, "settings.json")
d = cargar(p)
antes = json.loads(json.dumps(d))
perm = d.setdefault("permissions", {})
deny = perm.setdefault("deny", [])
deny += [r for r in base["permisos"]["deny"] if r not in deny]
if "allow" in perm:
    perm["allow"] = [r for r in perm["allow"] if r not in quitar]
hooks = d.setdefault("hooks", {})
for evento, lista in base["hooks"].items():
    actuales = hooks.setdefault(evento, [])
    comandos = {h.get("command") for e in actuales for h in e.get("hooks", [])}
    for i, h in enumerate(lista):
        if h["command"] not in comandos:
            actuales.insert(i, {"matcher": h["matcher"], "hooks": [{"type": "command", "command": h["command"]}]})
guardar(p, d, antes)

pl = os.path.join(CLAUDE, "settings.local.json")
l = cargar(pl)
if l:
    antes_l = json.loads(json.dumps(l))
    if "allow" in l.get("permissions", {}):
        l["permissions"]["allow"] = [r for r in l["permissions"]["allow"] if r not in quitar]
    guardar(pl, l, antes_l)
