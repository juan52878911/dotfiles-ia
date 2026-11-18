# F5 adaptar `delega wave` (T6) y el relevo (M3) a la interfaz estricta de `delega mem`

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` en 6e1cfc1 (todo integrado: fase 1, integrate --merge, mem estricto F2, daemon F4, clean, bundle, wave, relevo). La suite está **roja**: 314+ pruebas con 3 errores en `test_wave.py` y 7 fallos en `test_relevo.py`.

## Contexto
T6 y M3 se escribieron contra la primera versión de `mem`. F2 la hizo estricta y F4 adaptó el daemon (`queue.py`, úsalo como modelo):
- `--org`/`$DELEGA_MEM_ORG` obligatorio en toda escritura y en claim/heartbeat/release; base por defecto `memoria-<org>.db` (`db_path(path, org=)`);
- `--owner`/`$DELEGA_MEM_OWNER` obligatorio en claim/heartbeat/release;
- `set --desde <estado>` compare-and-set (rc 9) y transiciones válidas (p. ej. `pendiente → rojo` ya no vale; `lanzada → rojo` sí; `--forzar` solo cuando de verdad se corrige a mano);
- rc 10 = base bloqueada (reintentar); TTL finito 0 < ttl ≤ 3600.
Lee el docstring de `src/delega/mem.py` y la clase `MemClient` de `src/delega/queue.py`.

Además, al integrar M3 la clave `mem.usage` de `i18n/{en,es}.json` se quedó con la versión de F2 y le faltan las órdenes `backup`, `promote` y `event` que añadió M3: complétala.

## Terminado cuando
- `wave.py` y `relevo.py` usan la interfaz estricta (org y owner siempre; `--desde` en cada cambio de estado que pueda competir con otro orquestador; rc 10 reintentado); no se relaja `mem.py` salvo para arreglar `backup`/`promote`/`event` si chocan con `db_path(..., org=)`.
- Las pruebas de `test_wave.py` y `test_relevo.py` pasan; ajústalas solo donde la interfaz cambió a propósito, sin quitar lo que comprobaban.
- `mem.usage` lista todas las órdenes en los dos idiomas.
- `cd delega && python3 -m unittest discover -s tests` en **verde** (todas).
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/wave.py`, `delega/src/delega/relevo.py`, `delega/tests/test_wave.py`, `delega/tests/test_relevo.py`, la clave `mem.usage` de i18n, y en `delega/src/delega/mem.py` solo `backup`/`promote`/`event`.
- **No tocar**: `queue.py`, `launch.py`, `jobs.py`, `kindling.py`, `integrate.py`, `clean.py`.
- Rama `claude/orq-f5`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ 5 USD · 250 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste, sha, pruebas (números), qué quedó sin verificar.
