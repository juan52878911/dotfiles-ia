# M2 lease y daemon: `delega tick --daemon` que solo actúa si tiene la lease de la org

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (incluye `mem.py` de M1: lee su docstring, es la interfaz).

## Contexto
El orquestador del Mac se trabó y nadie siguió. Cada máquina de una org correrá un daemon; solo el dueño de la lease `orquestador:<org>` (vence a los 90 s) reconcilia, lanza e integra. La base se resuelve por `ws contexto` → org → `[memoria.<org>] host=…, path=…` en la config (Trycore: dev-trycore; personal: `orq@<servidor-memoria>`, CT 110).

## Notas de M1 (013195c)
- Interfaz: `claim <rol> --ttl S` (0 toma/renueva, 9 ocupada), `heartbeat`, `release`, `next <flujo>` → `{"tareas":[…],"bloqueadas":[…]}`, `set <tarea> --estado … --job-id/--nodo/--rama`. Códigos 0/2/9.
- El dueño es `host:pid`: un daemon reiniciado pierde su lease hasta que vence. Expón un `--owner` estable (la API ya acepta `owner=`) y mantén las transacciones cortas (< busy_timeout 5000 ms).

## Terminado cuando
- `delega tick --daemon --interval 30`: toma o renueva la lease con `mem claim`; si no la tiene, no hace nada; si la tiene, hace lo de `tick` de hoy leyendo las tareas de la base (no de `queue.jsonl`), con `queue.jsonl` como respaldo de solo lectura para migrar lo que haya.
- `delega mem install-daemon`: genera (sin instalar sin `--yes`) la unidad systemd `--user` y el plist de launchd.
- Sección `[memoria.<org>]` en `config.py`, con acceso local si `host` es esta máquina y `ssh host delega mem … --json` si no.
- `tests/test_tick_daemon.py` (nuevo): con dos daemons sobre la misma base solo uno lanza; matando al dueño, el otro toma la lease en < 2 intervalos tras vencer; una base Trycore rechaza una tarea con perfil personal (rc 2). Suite en verde.

## Zona
- `delega/src/delega/queue.py` (tick), `delega/src/delega/config.py` (sección `memoria`), `delega/tests/test_tick_daemon.py` (nuevo), claves `tick.` en i18n.
- **No tocar**: `mem.py` salvo añadir una función que falte (dilo en el informe), `launch.py`, `kindling.py`, `clean.py`.
- Rama `claude/orq-daemon`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste ejecutando (con números), qué quedó sin verificar y qué recomiendas.
