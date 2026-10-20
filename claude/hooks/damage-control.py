#!/usr/bin/env python3
"""PreToolUse(Bash|Write|Edit|MultiEdit): suelo de seguridad (patrón damage-control de IndyDevDan).
Dos niveles:
  - BLOQUEAR (exit 2, el motivo vuelve al agente): destrucción irreversible o de alcance amplio.
  - PREGUNTAR (permissionDecision "ask"): acciones legítimas pero con consecuencias fuera de la sesión.
Mira el comando entero, así que también caza lo que va dentro de bash -c "…" o tras && ; |.
Desactivar para una sesión: DAMAGE_CONTROL=0 en el entorno de Claude Code (no en el comando)."""
import json, os, re, shlex, sys

HOME = os.path.expanduser("~")
BOVEDA = os.path.join(HOME, "Documents", "Desarrollo")

BLOQUEAR = [
    (r"\bgit\s+push\b[^;&|\n]*\s(--force(?!-with-lease)|-f)\b", "git push --force reescribe la historia remota; usa --force-with-lease si de verdad hace falta"),
    (r"\bgit\s+reset\s+[^;&|\n]*--hard\b", "git reset --hard descarta cambios sin vuelta atrás; usa git stash o una rama"),
    (r"\bgit\s+clean\s+[^;&|\n]*-[a-zA-Z]*f", "git clean -f borra ficheros sin seguimiento sin vuelta atrás"),
    (r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE\s+TABLE)\b", "DROP/TRUNCATE destruye datos; hazlo tú a mano tras un backup"),
    (r"\bmkfs(\.\w+)?\b|\bdd\s+[^;&|\n]*of=/dev/|\bdiskutil\s+(erase\w*|partitionDisk|zeroDisk)\b", "operación de disco destructiva"),
    (r"\bchmod\s+-R\s+0?777\b", "chmod -R 777 abre permisos de forma masiva"),
    (r"\b(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b", "descargar y ejecutar con | sh: baja el script, léelo y ejecútalo aparte"),
    (r"\bsudo\s+rm\b", "rm con sudo"),
]

PREGUNTAR = [
    (r"\bgit\s+branch\s+[^;&|\n]*-D\b", "git branch -D borra una rama sin comprobar si está fusionada"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.\s*($|[;&|])", "descarta todos los cambios sin commit del directorio"),
    (r"\b(terraform|tofu)\s+destroy\b|\bkubectl\s+delete\b|\bdocker\s+(system|volume)\s+prune\b|\bpct\s+destroy\b|\bqm\s+destroy\b", "destruye infraestructura o volúmenes"),
    (r"\bgh\s+repo\s+delete\b|\bgh\s+release\s+delete\b", "borrado en GitHub"),
]

# Reglas propias de tus máquinas (hosts de producción, rutas de clientes…): fuera del repo, en un JSON local
# {"bloquear": [[regex, motivo], …], "preguntar": [[regex, motivo], …]}.
LOCAL = os.path.expanduser(os.environ.get("DAMAGE_CONTROL_LOCAL", "~/.config/dotfiles-ia/damage-control.local.json"))
try:
    with open(LOCAL) as f:
        _extra = json.load(f)
    BLOQUEAR += [tuple(x) for x in _extra.get("bloquear", [])]
    PREGUNTAR += [tuple(x) for x in _extra.get("preguntar", [])]
except (OSError, ValueError):
    pass

RUTAS_ESCRITURA_BLOQUEADAS = [os.path.join(HOME, ".ssh"), os.path.join(HOME, ".aws"), os.path.join(HOME, ".gnupg"), "/etc", "/System"]


def rm_peligroso(cmd):
    """rm recursivo y forzado sobre algo de alcance amplio: /, ~, *, ., .., o rutas a 2 niveles o menos del home."""
    for m in re.finditer(r"(?:^|[;&|(\s'\"`])rm\s+([^;&|\n)'\"`]*)", cmd):
        try:
            w = shlex.split(m.group(1))
        except ValueError:
            continue
        flags = "".join(x.lstrip("-") for x in w if x.startswith("-") and not x.startswith("--"))
        largas = {x for x in w if x.startswith("--")}
        if not (("r" in flags or "R" in flags or "--recursive" in largas) and ("f" in flags or "--force" in largas)):
            continue
        for t in (x for x in w if not x.startswith("-")):
            p = os.path.expanduser(t.replace("$HOME", HOME))
            if t in ("/", "~", "~/", "*", ".", "..", "./", "../", "$HOME") or p.rstrip("/") in ("", HOME):
                return f"rm -rf sobre «{t}»: alcance demasiado amplio"
            ap = os.path.abspath(p)
            if ap.startswith(HOME + os.sep) and len(os.path.relpath(ap, HOME).split(os.sep)) <= 2:
                return f"rm -rf sobre «{t}»: carpeta de primer nivel del home; borra rutas concretas"
            if ap == BOVEDA or ap.startswith(BOVEDA + os.sep):
                return "rm -rf dentro de la bóveda de Obsidian: no se borra; marca la nota como hecha o archívala"
            if ap.count(os.sep) <= 2 and not ap.startswith(("/tmp", "/private/tmp", "/var/folders")):
                return f"rm -rf sobre «{t}»: ruta de sistema o demasiado alta"
    return None


def responder_preguntar(motivo):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                             "permissionDecisionReason": f"damage-control: {motivo}"}}))
    sys.exit(0)


def bloquear(motivo):
    print(f"damage-control bloqueó la acción: {motivo}. Si de verdad hace falta, pídele a Juan que la ejecute él.", file=sys.stderr)
    sys.exit(2)


def main():
    if os.environ.get("DAMAGE_CONTROL") == "0":
        return
    ev = json.load(sys.stdin)
    herramienta = ev.get("tool_name", "")
    entrada = ev.get("tool_input") or {}
    if herramienta == "Bash":
        cmd = entrada.get("command", "")
        motivo = rm_peligroso(cmd)
        if motivo:
            bloquear(motivo)
        for patron, motivo in BLOQUEAR:
            if re.search(patron, cmd, re.I if "DROP" in patron else 0):
                bloquear(motivo)
        for patron, motivo in PREGUNTAR:
            if re.search(patron, cmd):
                responder_preguntar(motivo)
    elif herramienta in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        ruta = os.path.abspath(os.path.expanduser(entrada.get("file_path", "") or entrada.get("notebook_path", "")))
        for r in RUTAS_ESCRITURA_BLOQUEADAS:
            if ruta == r or ruta.startswith(r + os.sep):
                bloquear(f"escritura en {r} (claves, credenciales o sistema)")


if __name__ == "__main__":
    main()
