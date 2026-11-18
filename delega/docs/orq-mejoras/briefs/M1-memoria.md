# M1 memoria: `delega mem`, la memoria central de orquestación en SQLite

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` en 32048d0.

## Contexto
Hoy la cola de delega es `queue.jsonl` en el estado del Mac, con `flock` local (`delega/src/delega/queue.py`), y guarda `repo` y `brief` como rutas del Mac. Si el orquestador del Mac falla, nadie más puede seguir. Queremos una base SQLite por organización (una para Trycore, otra para personal; nunca mezcladas) que vive en un servidor y se usa por ssh con `ssh <servidor> delega mem <orden> --json`. En el servidor el acceso es directo al fichero. Nunca se abre por un sistema de ficheros de red.

Este trabajo solo crea el módulo y su CLI. El daemon con lease (M2) y el relevo (M3) llegan después y se apoyan en esta interfaz: que sea estable y esté documentada en el docstring del módulo.

## Hipótesis
- Con `sqlite3` de la biblioteca estándar, `BEGIN IMMEDIATE` y `busy_timeout`, dos procesos que reclaman la misma lease se serializan sin corrupción. Refútalo con una prueba de dos procesos (`multiprocessing`) antes de construir encima.

## Terminado cuando
- `delega/src/delega/mem.py` con:
  - esquema y migración idempotente (`PRAGMA user_version`); tablas `flujos` (id, org, repo **remoto git**, rama destino, estado, plan), `tareas` (id, flujo, ola, brief **texto**, zona, deps JSON, run_args JSON, estado ∈ pendiente|lanzada|ok|rojo|arreglo|bloqueada|hecha, intentos, job_id, nodo, rama, actualizado), `leases` (rol, dueño `host:pid`, vence_en, latido), `eventos` (solo añadir: ts, quién, qué, JSON);
  - órdenes `delega mem init|add-flow|add-task|next|set|claim|heartbeat|release|log|export`, todas con `--db <ruta>` (por defecto `$DELEGA_MEM_DB` o `~/.local/state/delega/memoria.db`) y salida `--json`;
  - `next <flujo>` devuelve las tareas `pendiente` cuyas deps están `hecha`;
  - `claim <rol> --ttl S` sale 0 si toma o renueva la lease, **9** si la tiene otro y no ha vencido;
  - `add-flow`/`add-task` **rechazan** con rc 2 un `repo` que sea una ruta local (debe ser un remoto: `git@…`, `https://…` o `owner/name`) y una org que no coincida con la de la base (`init --org <org>` la fija).
- Una línea en `delega/src/delega/__main__.py` que registra `mem`.
- `delega/tests/test_mem.py` (nuevo), con pruebas que fallan sin el módulo: dos procesos reclaman la misma lease y gana exactamente uno (el otro rc 9); una tarea añadida por un proceso la lee otro con `--json`; ruta local rechazada; org distinta rechazada; `next` respeta deps; la lease vencida la toma otro.
- `cd delega && python3 -m unittest discover -s tests` en verde y `ruff check delega/src/delega/mem.py` limpio si `ruff` está.
- Criterio de parada: 2 intentos sin verde → para y describe el fallo.

## Zona
- `delega/src/delega/mem.py` y `delega/tests/test_mem.py` (nuevos), una línea en `delega/src/delega/__main__.py`, claves nuevas con prefijo `mem.` en `delega/src/delega/i18n/{en,es}.json`.
- **No tocar**: `queue.py` (su migración a la base es de M2), `integrate.py`, `launch.py`, `kindling.py`, `skills/**`.
- Rama `claude/orq-memoria`; commitea; sin push ni merge a `main`.

## Tope
- ≤ 60 min de reloj · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, las pruebas y su resultado (números), la interfaz final (órdenes y códigos de salida), qué quedó sin verificar y qué recomiendas para M2.
