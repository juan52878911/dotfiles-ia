# ia-config

Mi configuración de IA en un solo sitio. De momento, Claude Code: hooks, subagentes y las reglas que deben estar siempre en `settings.json`. Los dotfiles (`~/dotfiles`) lo consumen como input del flake.

## Qué hay

| Ruta | Qué es |
|---|---|
| `claude/hooks/damage-control.py` | Suelo de seguridad (patrón *damage-control* de IndyDevDan): bloquea lo irreversible (`rm -rf` amplio, `git push --force`, `reset --hard`, `DROP`, borrado de discos, `curl \| sh`, escrituras en `~/.ssh`…) y pregunta antes de lo delicado (`git branch -D`, `terraform destroy`, `docker prune`…). |
| `claude/hooks/no-cat-grande.py` | Impide volcar ficheros grandes enteros al contexto. |
| `claude/agents/` | Subagentes, cada uno con su modelo (Sonnet o Haiku; Opus queda para la sesión principal). |
| `claude/settings-base.json` | Lo que se garantiza en `~/.claude/settings.json`: reglas `deny`, hooks y comodines que se quitan de `allow`. |
| `claude/fusionar-settings.py` | Fusiona lo anterior sin pisar lo que escriben Claude Code, Nod u Orbit. Idempotente. |
| `nix/modulo.nix` | Módulo de home-manager (`programs.ia-config`). |
| `tests/probar-damage-control.py` | Casos de prueba del hook. |

## Uso con home-manager

```nix
# flake.nix de los dotfiles
inputs.ia-config.url = "git+file:///Users/<usuario>/Documents/GitHub/ia-config";   # o github:… cuando esté publicado (path: falla: su hash incluye .git)

# home.nix
imports = [ ia-config.homeManagerModules.default ];
programs.ia-config = {
  enable = true;
  checkout = "/Users/<usuario>/Documents/GitHub/ia-config";   # editable sin rebuild
};
```

## Reglas locales (no versionadas)

Hosts de producción, rutas de clientes o cualquier cosa de empresa van en
`~/.config/ia-config/damage-control.local.json`, que el hook lee si existe:

```json
{ "preguntar": [["\\bssh\\s+[^;&|\\n]*\\bmi-servidor-prod\\b", "producción"]],
  "bloquear":  [] }
```

**Nada de Trycore se versiona aquí.** La configuración de la empresa vive en sus propios repos.

## Probar

```bash
DAMAGE_CONTROL_LOCAL=/dev/null python3 tests/probar-damage-control.py
python3 claude/fusionar-settings.py --seco
```

Desactivar el hook en una sesión: `DAMAGE_CONTROL=0` en el entorno de Claude Code.
