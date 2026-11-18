# F10 costura daemon ↔ wave ↔ relevo (hallazgos de R3)

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (383 pruebas OK en en y es). La revisión R3 (Opus) dice «no apto»: los arreglos de la ronda 2 se hicieron en paralelo y no encajan entre sí. Arreglas **la costura**: lee juntos `src/delega/queue.py` (daemon), `src/delega/wave.py`, `src/delega/relevo.py` y `src/delega/launch.py` antes de tocar nada.

## Hallazgos
1. **Bloqueante** `queue.py:549` `_reconcile_tasks`: `state, jid = env.job_state(t)` pero `job_state` devuelve `(state, jid, sha)` → `ValueError` tragado por `except Exception`: ninguna `lanzada` pasa nunca a `ok`/`rojo`. La prueba `test_tick_daemon.py:246` usa un mock de 2 valores. Arreglo: usar la tupla de 3 (guardar el sha) y que la prueba use el `job_state` real con un directorio de trabajo falso.
2. **Bloqueante** `queue.py:530,547`: el daemon crea `wave.Env(os.getcwd())` sin `org` y sin `check_repo_dir`/`check_profiles`: lanza, integra y hace `rev-parse` en el cwd del daemon, `_fetched_sha` da None y todo job ok acaba `rojo`; los briefs caen en `wave/_/`. Arreglo: `Env(<checkout del repo del flujo>, org=client.org)` (con `relevo.origin(cfg, fl["repo"])` en un nodo o el checkout local que corresponda en el Mac) y las mismas comprobaciones que la línea de órdenes de `wave`.
7. **Importante** `queue.py:642-660` `_escalate_one` no usa `relevo.escalate` (orden `set`+`spawn` y luego evento `once`, `escalado_fallido` con reintento): ese arreglo es código muerto en producción; además `spawn` va sin `org` (`escalado.json` con `PROFILE: ""`). Arreglo: reutilizar `relevo.escalate` limitado a una tarea por ciclo, pasando `client.org`.
8. **Importante** `relevo.py:37,228-230`: la garantía real contra push/merge es el modo plan + lista cerrada de herramientas, no el entorno. Comprueba en real (en esta VM, con `claude --permission-mode plan -p "ejecuta: delega mem --help"` y la lista cerrada) si `Bash(delega mem *)` se puede ejecutar en modo plan. Si no, usa `--permission-mode default` con la lista cerrada `--allowedTools` (lectura + `Bash(delega mem *)`) y `--disallowedTools` para `Bash(git *)`, `Bash(gh *)`, `Edit`, `Write`. Documenta en el docstring qué garantiza qué.
9. **Importante** `wave.py:262` `_fetched_sha` toma un `fetch.log` no vacío como fallo (un «Warning: Permanently added…» de ssh convierte un ok en rojo). Arreglo: que `launch.py` escriba `FETCHED=<sha>` en `meta` al traer la rama (también en el camino `refs/delega/<id>`), y que `wave` y `clean` lean `FETCHED` (con la regla de `fetch.log` solo como respaldo para trabajos viejos sin esa clave).
10. **Importante** `wave.py:457` → `integrate.py:215`: integrar por sha deja `job_for_branch` en None (audit con `job=None`, sin `integrate.log`, merge que nombra el sha). Arreglo: `integrate <repo> <rama> --sha <sha>` (nueva opción: verifica y fusiona ese sha, pero el job y el mensaje usan la rama).
11. **Menor** `clean.py:53`: queda resuelto con el 9.
12. **Menor** `wave.py:413`: el reintento desde `rojo` no adjunta el final del log del fallo; añade las últimas ~80 líneas de `progress.log`/`stderr.log` del trabajo al brief del reintento.
13. **Menor** `relevo.py` `spawn`: el perfil del escalado es el primero de la org; usa el perfil de la tarea (o el de `repo_match` del repo del flujo).
15. **Menor** `wave.py` `check_profiles`: una tarea sin perfil usa `profile_for(repo)` sin comprobar la org; compruébalo también.

## Reglas de ejecución (importante)
- Pruebas **siempre en primer plano** (Bash normal, timeout hasta 600000 ms). Nunca en segundo plano, Monitor ni esperando notificaciones: si terminas el turno esperando, delega cierra el trabajo y se pierde lo no commiteado.
- **Commitea en cuanto tus pruebas nuevas pasen**, antes de la suite completa; si la suite falla, arregla y otro commit.
- Las pruebas nuevas usan las funciones reales que se llaman entre módulos (no mocks con otra forma): R3 encontró un mock de 2 valores donde la función real devuelve 3.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo; para 1 y 2, una prueba de **un ciclo completo del daemon** con un flujo de olas: tarea `pendiente` → lanzada (con un `delega run` falso que crea un directorio de trabajo real con `meta`, `summary.json` rc 0 y `FETCHED=`) → `ok` → integrada (`integrate` falso con rc 0) → `hecha`, todo con las funciones reales de `wave` y `queue`.
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y con `DELEGA_LOCALE=es`, los dos en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/queue.py`, `wave.py`, `relevo.py`, `clean.py`, `integrate.py` (solo `--sha`), `launch.py` (solo escribir `FETCHED=`), sus pruebas, claves en i18n. **No tocar** `mem.py` ni `jobs.py`/`kindling.py`.
- Rama `claude/orq-f10`; commitea; sin push ni merge.

## Tope
- ≤ 75 min · ≤ 8 USD · 400 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo (número), sha, pruebas (números), resultado real de la prueba del modo plan (punto 8), qué quedó sin verificar.
