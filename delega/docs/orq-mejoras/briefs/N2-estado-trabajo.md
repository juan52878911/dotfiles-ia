# N2 estado del trabajo en la base: el nuevo dueño sabe en qué va una tarea lanzada por otro

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` (incluye `delega mem`, `tick --daemon` con lease por org, `wave`, `relevo` y los arreglos del piloto de relevo del 18-nov).

## Contexto
Piloto del 18-nov (org Trycore): dos daemons (`trycorepc`, `dev-trycore`) se disputan la lease `orquestador:<org>`; el que la tiene lanza tareas en un nodo. El directorio del trabajo (`meta`, `summary.json`, `fetch.log`) vive en el `state_dir` del **host que lanzó**. Si ese daemon muere con una tarea en `lanzada`, el nuevo dueño llama a `wave.Env.job_state` (`src/delega/wave.py`, `_job_dir` ~l. 291 y `job_state` ~l. 320) desde `_reconcile_tasks` (`src/delega/queue.py` ~l. 557): no encuentra el directorio y la da por `fail` (o `job_sin_directorio` → `necesita_decision`), y con `pend:<pid>:…` mira el pid **en su propia máquina**. Resultado: relanza trabajo que seguía vivo o escala a Claude sin motivo.

Lee primero el docstring de `src/delega/mem.py` (interfaz estable), `_reconcile_tasks` y `MemClient` en `queue.py`, y `job_state`/`_job_dir`/`_fetched_sha` en `wave.py`.

## Terminado cuando
- La tarea guarda en la base **quién la lanzó** (columna nueva `lanzador` en `tareas`, migración idempotente como la de `leases.epoca`; `mem set … --lanzador <dueño>`), escrito por el daemon al pasar a `lanzada`. El valor es el `owner` del daemon (en un nodo coincide con su alias ssh de `[nodes]`).
- `_reconcile_tasks`: si la tarea la lanzó otro dueño y el directorio no está aquí, pregunta su estado al lanzador por ssh con una orden nueva y de solo lectura, `delega job-state --task <id> --rama <rama> --job-id <id> --json` (devuelve `{"estado": running|ok|fail|queued, "job_id": …, "sha": …}` usando la misma lógica de `job_state`). Si el lanzador no responde (ssh rc 255), la tarea **sigue en `lanzada`** y se reintenta en el siguiente ciclo; solo tras `N` ciclos seguidos sin respuesta (constante, p. ej. 10 ≈ 5 min) pide decisión con `necesita_decision` motivo `lanzador_caido` (una vez, `--once`).
- Un `pend:<pid>:…` de otro host nunca se juzga con el pid local.
- Si el lanzador responde `ok` con sha, el nuevo dueño trae la rama de ese host (el bare del nodo donde corrió el trabajo ya la tiene: `NODE` en la respuesta) o, si no puede, la deja en `ok` con el sha y avisa; no la marca `rojo`.
- Pruebas nuevas en `tests/test_n2_lanzador.py` con un `ssh` falso que ejecuta `delega` contra otro `state_dir`: (a) daemon A lanza, muere; daemon B reconcilia `running` y luego `ok` sin relanzar; (b) lanzador caído: la tarea sigue en `lanzada` N−1 ciclos y al N-ésimo hay **un** evento `necesita_decision`; (c) un `pend:` de otro host con un pid que existe localmente no cuenta como vivo; (d) la migración de la columna sobre una base antigua.
- `cd delega && python3 -m unittest discover -s tests` en verde (y con `DELEGA_LOCALE=es`). Pruebas en **primer plano**; commitea antes de lanzar la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/queue.py` (solo `_reconcile_tasks`, `MemClient.set` y el sitio donde se pasa a `lanzada`), `delega/src/delega/wave.py` (solo `job_state`, `_job_dir`), `delega/src/delega/mem.py` (columna, migración, `set --lanzador`), un módulo nuevo `delega/src/delega/jobstate.py` para la orden `job-state` y su registro en `__main__.py`, `delega/src/delega/i18n/*.json` (claves `n2.` o `jobstate.`), `delega/tests/test_n2_lanzador.py`.
- **No tocar**: `sync.py`, `relevo.py`, `launch.py`, `integrate.py`, `kindling.py`.
- Rama `claude/orq-n2`; commitea; sin push ni merge.

## Tope
- ≤ 75 min · ≤ 8 USD · 500 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, pruebas y resultado (números), qué quedó sin verificar.
