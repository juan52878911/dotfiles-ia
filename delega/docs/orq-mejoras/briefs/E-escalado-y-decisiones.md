# E1–E4 que una decisión pedida por el relevo se pueda tomar de verdad

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` (`57956c0`). Un solo trabajo: los cuatro tocan las mismas funciones.

## Contexto
Piloto real del 18-nov (org Trycore; base SQLite en `trycorepc`, daemons en `trycorepc` y `dev-trycore`). Con `kill -9` al dueño y el trabajo vivo, el relevo funcionó; pero la decisión que se pidió después no se pudo tomar:
- **E1** El Claude escalado (`relevo.spawn`/`claude_argv`, `relevo.py` ~300-354, `ALLOWED_TOOLS` l. 36) corre en el host del daemon dueño. Su `delega mem` abre la base **local** por defecto; la de la org está en otro host (`cfg.memoria_for(org)` → `host`, `path`, `local`). Respondió «no tengo la base» y no decidió nada.
- **E2** El evento `necesita_decision` motivo `arreglo_fallido` (`wave.py` ~579) y `rojo_repetido` (~593) no llevan `registro` (la cola del log del trabajo), y `job_sin_directorio`/`lanzador_*` (`queue.py` ~621) tampoco dicen qué se sabe. El Claude escalado no pudo ver la causa.
- **E3** El nuevo dueño vio `ok` (END en `meta`) mientras el `delega run` original aún cerraba; el trabajo de arreglo de `wave` se **rechazó** al lanzar («la rama … está abierta en el worktree del trabajo …, que no ha terminado», rc `launch.REFUSED_RC` = 2) y eso contó como «arreglo falló» → `bloqueada`. Un lanzamiento rechazado no es un arreglo fallido.
- **E4** El contador de ciclos sin respuesta del lanzador (`_LANZADOR_FALLOS`, `queue.py` ~582-695) vive en memoria: un daemon que se reinicia a menudo nunca llega a `LANZADOR_MAX_FALLOS`.
Lee primero el docstring de `src/delega/mem.py`, `relevo.py` (`brief_for`, `claude_argv`, `spawn`, `escalate`, `candidates`), `wave.py` (`step`, el paso de arreglos) y `queue.py` (`task_state`, `_reconcile_tasks`).

## Terminado cuando
- **E1** El brief del Claude escalado (`relevo.brief` en i18n) da el comando exacto para leer y escribir la base de la org: si la base es local, `delega mem … --db <ruta> --org <org>`; si es remota, `ssh <host> delega mem … --db <ruta> --org <org>` (con el `PATH="$HOME/.local/bin:$PATH"` que ya usa `MemClient._ssh`). `--allowedTools` añade **solo** ese prefijo (`Bash(ssh <host> delega mem *)` o el de la ruta local); siguen prohibidos `git`, `gh`, `Edit`, `Write`. Lleva `DELEGA_MEM_OWNER=<dueño>-claude` para que sus escrituras pasen la valla.
- **E2** Todo `necesita_decision` lleva `registro`: la cola (≤ `LOG_TAIL`) del log del último trabajo de esa tarea si existe (el del arreglo en `arreglo_fallido`), o una línea de qué se sabe (`job_id`, `lanzador`, último error). `brief_for` lo incluye.
- **E3** En `wave.step`, si el trabajo de arreglo termina con el rc de rechazo de lanzamiento (o su log contiene el rechazo por rama abierta), la tarea vuelve a `ok` (CAS desde `arreglo`, sin sumar intento) y se reintenta la integración/arreglo en un paso posterior; solo tras 3 rechazos seguidos pide decisión motivo `lanzamiento_rechazado` con el registro.
- **E4** El contador vive en la base: un evento `lanzador_sin_respuesta` por ciclo fallido (o un campo en `tareas` vía orden nueva de `mem` si lo prefieres, con migración idempotente), y el recuento sale de la base; un reinicio del daemon no lo pone a cero. Se reinicia cuando el lanzador responde.
- Pruebas en `tests/test_e_escalado.py`, sin simular `task_state`, `job_state`, `integrate` ni `escalate` (sí ssh por host y `claude`/`delega run` falsos): (E1) base remota → el argv del Claude contiene `Bash(ssh <host> delega mem *)` y el brief el comando con `--db` y `--org`; base local → la ruta local; (E2) `arreglo_fallido` con `registro` = cola del log del arreglo; (E3) arreglo rechazado por rama abierta → `ok` y reintento, 3 rechazos → una decisión; (E4) 10 ciclos sin respuesta repartidos entre dos objetos `MemClient`/daemons distintos → una decisión.
- `cd delega && python3 -m unittest discover -s tests` en verde en `en` **y en `es` (suite completa en los dos)**. Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/relevo.py`, `wave.py`, `queue.py`, `mem.py` (solo si eliges el campo de E4: orden y migración al final), `delega/src/delega/i18n/*.json`, `delega/tests/test_e_escalado.py` y pruebas existentes que haya que adaptar.
- **No tocar**: `launch.py`, `integrate.py`, `sync.py`, `kindling.py`.
- Rama `claude/orq-e`; commitea; sin push ni merge.

## Tope
- ≤ 75 min · ≤ 8 USD · 500 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: cómo cerraste E1–E4, rama y sha, pruebas en en y es (números), qué quedó sin verificar.
