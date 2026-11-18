# P1 runner de Pi: que un trabajo con Pi + MiniMax sea un trabajo de delega como los demás

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras`.

## Contexto
Hoy Pi (`~/.local/bin/pi`, MiniMax M3) se lanza a mano en un worktree del Mac: no aparece en `delega ps`, no tiene topes ni eventos, y el relevo no lo ve. Regla fija: Pi/MiniMax **solo** en perfiles personales, nunca Trycore (`forbidden_engines = ["minimax","pi"]` en el perfil trycore).

## Terminado cuando
- `delega run … --engine pi [--model minimax/MiniMax-M3]`: lanza `pi -p --mode json` en un worktree (local o nodo), traduce su salida JSON a `events.jsonl` (inicio, herramientas, resultado), aplica `--cap-min` (corta el proceso) y escribe `summary.json` con estado y duración; `ps` lo muestra.
- Rechazo con rc 2 si el perfil tiene `pi` o `minimax` en `forbidden_engines`.
- Pruebas con un `pi` falso en `tests/fakes/`: un trabajo ok aparece en `ps` con sus herramientas; el tope corta; el perfil trycore rechaza. Suite en verde.

## Zona
- `delega/src/delega/engines/pi.py` (nuevo), el despacho mínimo en `launch.py` (solo la elección de motor), `tests/test_engine_pi.py` (nuevo), `tests/fakes/pi`, claves `engine.` en i18n.
- **No tocar**: `kindling.py`, `queue.py`, `mem.py`, `wave.py`.
- Rama `claude/orq-pi`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste ejecutando (con números), qué quedó sin verificar y qué recomiendas.
