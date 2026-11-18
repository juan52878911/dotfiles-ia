# T6 olas: `delega wave`, una ola entera sin bash ni orquestador mirando

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (ola 0: fase 1, `integrate --merge --node`, `delega mem`).

## Contexto
Las 32 olas de platform-fundations las condujo un bash de proyecto (`.orquestador/finde.sh`, con `sleep`): paralelismo 1,17 y la integración en serie con un agente por rama (mediana 18 min, el 43 % del tiempo de cada ola). Queremos que una ola sea un dato en la memoria central y que delega la lleve sola. El daemon con lease (M2, rama `claude/orq-daemon`, aún no integrada) será quien llame a esto; tú solo necesitas la interfaz de `mem.py` (lee su docstring) y `integrate --merge` (rc 0 verde, 7 rojo, 8 conflicto).

## Terminado cuando
- `delega wave plan <flujo> <ola.tsv>`: lee un TSV (`id	brief	zona	deps	run_args`), comprueba **antes de escribir nada** que las zonas de las filas de una misma ola son disjuntas (globs), y lo carga en la base como tareas de esa ola (`mem add-task`). Zonas solapadas → rc 2 y no escribe ninguna fila.
- `delega wave step <flujo> [--par N] [--into <rama>] [--node <n>]`: un paso idempotente (lo llamará el daemon cada 30 s):
  1. reconcilia las tareas `lanzada` con el estado de sus trabajos (ok → `ok`, fallo → `rojo`);
  2. lanza las tareas listas (`mem next`) hasta `--par` en vuelo, marcándolas `lanzada` con job_id/nodo/rama;
  3. cuando todas las tareas de la ola están `ok`, fusiona sus ramas en `--into` una a una con `delega integrate --merge --node` y las marca `hecha`;
  4. si una integración sale 7 (rojo), lanza **un** trabajo de arreglo con el registro de la suite en el brief y marca la tarea `arreglo`; si el arreglo vuelve a salir rojo, `bloqueada` y evento `necesita_decision`;
  5. 8 (conflicto) → `bloqueada` con los ficheros en el evento.
- `delega wave status <flujo>`: tabla por ola y tarea.
- `tests/test_wave.py` (nuevo) con los falsos de `tests/fakes/` y `delega run`/`integrate` sustituidos por dobles: ola de 3 filas verde termina con las 3 `hecha` sin lanzar arreglos; una fila roja lanza exactamente un arreglo; dos rojos seguidos → `bloqueada`; zonas solapadas rechazan la ola entera; `step` dos veces seguidas no lanza dos veces la misma tarea.
- Plantilla `delega/skills/delega/templates/es/ola.tsv` con un ejemplo comentado.
- `cd delega && python3 -m unittest discover -s tests` en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/wave.py` y `delega/tests/test_wave.py` (nuevos), una línea en `__main__.py`, `delega/skills/delega/templates/*/ola.tsv`, claves `wave.` en i18n.
- **No tocar**: `mem.py`, `queue.py`, `integrate.py`, `launch.py`, `kindling.py` (otros trabajos). Si necesitas algo de `mem.py` que no está, envuélvelo en `wave.py` y dilo en el informe.
- Rama `claude/orq-olas`; commitea; sin push ni merge.

## Tope
- ≤ 90 min · ≤ 10 USD · 500 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, pruebas y resultado (números), interfaz final, qué quedó sin verificar y qué necesita M3.
