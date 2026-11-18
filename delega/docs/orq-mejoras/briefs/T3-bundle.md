# T3 bundle: subir a kindling solo lo necesario (y las ramas que el brief nombra)

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (main + fase 1 + ola 0).

## Contexto
`delega run` en kindling sube un bundle con TODO el historial: Oriuno (303 MB) choca con el límite de 64 MB de `kling` («upload is over the 67108864 byte limit»). Además a la VM solo llega HEAD: un trabajo que necesitaba otra rama local (`claude/kling-fase1-b556de`) no la encontró y terminó sin hacer nada.

## Terminado cuando
- `push_head` (en `src/delega/kindling.py`): no sube nada si HEAD es ancestro de la base del dorado; si divergen, sube `HEAD --not <merge-base>`; si el bundle pasa de 60 MB, lo parte en trozos que llegan enteros.
- Opción `delega run --with-ref <rama>` (repetible): añade esas ramas al bundle y las crea en el clon de la VM.
- Pruebas en `tests/test_kindling.py` con `tests/fakes/kling-bin/kling` limitado a 64 MB: HEAD por detrás (0 bytes), divergente (solo lo propio), bundle de 70 MB que llega entero, `--with-ref` crea la rama en la VM. Fallan sin el cambio.
- `cd delega && python3 -m unittest discover -s tests` en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/kindling.py`, `delega/tests/test_kindling.py`, `delega/tests/fakes/kling-bin/kling`, la opción en `delega/src/delega/launch.py` (solo el parser y el paso al runner), claves `kindling.` en i18n.
- **No tocar**: `queue.py`, `mem.py`, `integrate.py`, `clean.py`.
- Rama `claude/orq-bundle`; commitea; sin push ni merge a `main`.

## Tope
- ≤ 45 min · ≤ 4 USD · 200 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste ejecutando (con números), qué quedó sin verificar y qué recomiendas.
