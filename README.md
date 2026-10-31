# dotfiles-ia

Mi configuración de IA en un solo sitio. De momento, Claude Code: hooks, subagentes y las reglas que deben estar siempre en `settings.json`. Los dotfiles (`~/dotfiles`) lo consumen como input del flake.

## Qué hay

| Ruta | Qué es |
|---|---|
| `claude/hooks/damage-control.py` | Suelo de seguridad (patrón *damage-control* de IndyDevDan): bloquea lo irreversible (`rm -rf` amplio, `git push --force`, `reset --hard`, `DROP`, borrado de discos, `curl \| sh`, escrituras en `~/.ssh`…) y pregunta antes de lo delicado (`git branch -D`, `terraform destroy`, `docker prune`…). |
| `claude/hooks/no-cat-grande.py` | Impide volcar ficheros grandes enteros al contexto. |
| `claude/hooks/delega-scan.py` | Antes de `git commit`, `git push` o un `gh` que publica, pasa `delega scan` por lo que sale y bloquea si hay términos prohibidos del perfil. |
| `claude/hooks/delega-pr-gate.py` | Bloquea `gh pr create` desde `delega/*` u `orq/*` sin un `delega integrate` correcto de esa rama en su sha actual. |
| `claude/hooks/modelo-orquestador.py` | Avisa al arrancar si una sesión orquestadora de delega usa un modelo `[1m]`. |
| `claude/agents/` | Subagentes, cada uno con su modelo (Sonnet o Haiku; Opus queda para la sesión principal). |
| `claude/settings-base.json` | Lo que se garantiza en `~/.claude/settings.json`: reglas `deny`, hooks y comodines que se quitan de `allow`. |
| `claude/fusionar-settings.py` | Fusiona lo anterior sin pisar lo que escriben Claude Code, Nod u Orbit. Idempotente. |
| `claude/activar-plugin.py` | Declara un marketplace local de Claude Code y activa su plugin en `~/.claude` y en otros `CLAUDE_CONFIG_DIR`; lo usa `programs.dotfiles-ia.tokeneconomizer` (TokenEconomizer desde un input del flake, en todas las cuentas y en Pi). |
| `nix/modulo.nix` | Módulo de home-manager (`programs.dotfiles-ia`). Con `chronoIa.enable`, instala `chrono-ia` (chrono sobre un índice de operaciones de IA: delega, Nod y transcripciones de Claude) y un agente de launchd que lo sincroniza cada 5 min. |
| `tests/probar-damage-control.py` | Casos de prueba del hook. |
| `delega/` | Orquestador de trabajadores de Claude Code en otras cuentas y máquinas (topes, presupuesto, cupo, auditoría). Genérico y sin datos personales: la configuración vive fuera, en `~/.config/delega`. Ver `delega/README.md`. |

## Uso con home-manager

```nix
# flake.nix de los dotfiles
inputs.dotfiles-ia.url = "github:juan52878911/dotfiles-ia";   # en local: git+file:///ruta/al/clon (path: no, su hash incluye .git)

# home.nix
imports = [ dotfiles-ia.homeManagerModules.default ];
programs.dotfiles-ia = {
  enable = true;
  checkout = "/Users/<usuario>/Documents/GitHub/dotfiles-ia";   # editable sin rebuild
};
```

## Reglas locales (no versionadas)

Hosts de producción, rutas de clientes o cualquier cosa de empresa van en
`~/.config/dotfiles-ia/damage-control.local.json`, que el hook lee si existe:

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
