# M3 relevo: que otro equipo de la org siga orquestando si el Mac cae

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (incluye `delega mem`, `tick --daemon` con lease por org de M2, `integrate --merge --node` con los arreglos de F1, y `delega clean`).

## Contexto
El fin de semana el orquestador del Mac se trabó y nadie siguió. Ya existe la memoria (SQLite por org, por ssh) y el daemon con lease. Falta que un **nodo** (trycorepc o dev-trycore para Trycore; el CT 110 `orq@<servidor-memoria>` para personal) pueda hacer de orquestador sin el Mac: tener la config del perfil, lanzar desde su repo bare y pedir ayuda a Claude solo cuando haga falta decidir. Lee primero `src/delega/queue.py` (daemon), `src/delega/mem.py` (interfaz en el docstring) y `src/delega/sync.py`.

## Terminado cuando
- `delega sync --profile <p> --with-orchestrator`: además del bundle de reglas, deja en el nodo `delega.pyz` y una config de orquestador **derivada** de la del Mac: el perfil, sus nodos, `[memoria.<org>]` y la política, **sin** `token_command` del llavero del Mac ni nada de otros perfiles (prueba: la config generada para `trycore` no contiene la palabra `personal` ni `security find-generic-password`).
- Origen del repo en el nodo: cuando el daemon corre en un nodo y la tarea trae `repo` remoto, lanza desde el bare `~/<workdir>/<repo>.git` de ese nodo (crearlo con `git clone --bare` si falta). Implementa esto en un módulo nuevo `src/delega/relevo.py` que `queue.py` llama; **no toques `launch.py`**.
- Al segundo `rojo` de una tarea (o evento `necesita_decision` de `delega wave`), el daemon dueño marca `bloqueada` y crea **una** petición de orquestador Claude: escribe en la base un evento `orquestador_claude` con el export del flujo y lanza `ws exec claude -p` (si `ws` existe; si no, `claude -p`) con un brief generado, modelo Sonnet, tope 3 USD. Nunca hace merge a `main` ni push. Una segunda petición para la misma tarea no se crea.
- Copia de seguridad: `delega mem backup --to <host:path>` (con `sqlite3 .backup` local y `scp`) y `delega mem promote --yes` (convierte una copia en la principal; sin `--yes`, rc 2).
- `tests/test_relevo.py` (nuevo) con los `ssh`/`ws`/`claude` falsos: un daemon «en un nodo» lanza una tarea cuyo brief solo está en la base; el segundo rojo crea exactamente una petición; la config derivada no filtra otros perfiles ni secretos; `promote` sin `--yes` no hace nada.
- Sección «Relevo» en `delega/skills/delega/SKILL.md` (≤ 25 líneas: qué corre en cada equipo, cómo se instala con `mem install-daemon`, cómo promover la copia).
- `cd delega && python3 -m unittest discover -s tests` en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/relevo.py` y `delega/tests/test_relevo.py` (nuevos), `delega/src/delega/sync.py`, en `delega/src/delega/queue.py` solo la llamada a `relevo`, en `delega/src/delega/mem.py` solo `backup`/`promote` añadidos al final, la sección de relevo de `delega/skills/delega/SKILL.md`, claves `relevo.` en i18n.
- **No tocar**: `launch.py`, `kindling.py`, `integrate.py`, `wave.py`, `clean.py`.
- Rama `claude/orq-relevo`; commitea; sin push ni merge.

## Tope
- ≤ 90 min · ≤ 10 USD · 500 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, pruebas y resultado (números), qué quedó sin verificar y qué hace falta para instalarlo de verdad en cada equipo.
