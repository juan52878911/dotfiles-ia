# F14 cierre de los tres huecos parciales de R7

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-n-int` (`543d3d0`, con F13). Arreglos pequeños y acotados.

## Contexto
R7 revisó F13 (relevo del orquestador entre nodos de una org) y dejó tres huecos:
- **(a)** El Mac (sin `[relevo] node`) deja `lanzador` vacío. Una tarea suya con id provisional `pend:<pid>:…` es juzgada por el nodo que toma el relevo como propia (`queue.py` ~670): `Env.job_state` busca el pid en el nodo (`wave.py` ~342-350), no lo halla → `fail` → `rojo` → se relanza.
- **(b)** Se graba como `lanzador` el **nombre** del nodo (`queue.py` ~295) y `_ask_lanzador` hace `ssh <nombre>` (~592); el alias está en `cfg.node(nombre)["ssh"]`.
- **(c)** `sync --with-orchestrator` del Mac empuja la rama destino con `+` (`sync.py` ~172): retrocede en el bare lo que el nodo integró y aún no está en GitHub.
Además `jobstate.not_found` es una clave i18n sin uso.

## Terminado cuando
- (a) Toda tarea `lanzada` guarda un `lanzador` no vacío: en un nodo, su nombre; en el Mac, un identificador explícito (p. ej. `general.owner_name` o `socket.gethostname()`) que no es un nodo de la config. En `task_state`: si el directorio del trabajo no está aquí y el `lanzador` no es este host (incluido vacío en tareas viejas y uno que no es nodo de la config), **nunca** se juzga el pid local: si el lanzador es un nodo, se pregunta por ssh; si no lo es, la tarea sigue `lanzada` y se pide `necesita_decision` motivo `lanzador_sin_ssh` (una vez).
- (b) Antes de preguntar, el nombre del nodo se traduce a su alias con `cfg.node(nombre)["ssh"]` (si el nombre no es nodo, se usa tal cual solo si no hay config de nodos; si no, caso (a)).
- (c) `sync --with-orchestrator` empuja la rama destino **sin `+`**; si el bare va por delante (push rechazado por no fast-forward), no fuerza: avisa (i18n) y sigue con el resto.
- Quita `jobstate.not_found` de los dos JSON si nadie la usa, o úsala.
- Pruebas nuevas en `tests/test_f14_cierre.py` (ssh falso por alias distinto del nombre, p. ej. nodo `sandbox` con `ssh = "kling-host"`): Mac lanza con `pend:`, muere, el nodo toma el relevo → no relanza y pide una decisión; nodo `sandbox` se pregunta por `kling-host`; bare por delante de `origin/main` → `sync` no lo retrocede. Sin simular `task_state`, `job_state` ni el `git push`.
- `cd delega && python3 -m unittest discover -s tests` en verde en `en` **y en `es` (suite completa en los dos)**. Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/queue.py`, `wave.py` (solo `job_state` si hace falta), `sync.py`, `delega/src/delega/i18n/*.json`, `delega/tests/test_f14_cierre.py` y pruebas existentes que haya que adaptar.
- **No tocar**: `mem.py`, `launch.py`, `integrate.py`, `relevo.py`.
- Rama `claude/orq-f14`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ 5 USD · 400 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤12 líneas: cómo cerraste (a), (b), (c), rama y sha, pruebas en en y es (números), qué quedó sin verificar.
