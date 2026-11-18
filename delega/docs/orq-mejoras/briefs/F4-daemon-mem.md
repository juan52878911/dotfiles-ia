# F4 adaptar `tick --daemon` (M2) a la interfaz estricta de `delega mem` (F2)

Repo `dotfiles-ia`, carpeta `delega/`. Rama `claude/orq-int-f2` (ya creada, HEAD 2ea0e88): `claude/orq-mejoras` + F2 fusionada; el conflicto de `mem.py` está resuelto a favor de F2 y `queue.py` ya pasa `DELEGA_MEM_ORG` y `org=` al cliente.

## Contexto
F2 hizo `delega mem` más estricto: `--org`/`$DELEGA_MEM_ORG` obligatorio en toda escritura y en claim/heartbeat/release; `--owner`/`$DELEGA_MEM_OWNER` obligatorio en claim/heartbeat/release; base por defecto `memoria-<org>.db`; `set --desde` como compare-and-set (rc 9); transiciones válidas; rc 10 = base bloqueada (reintentar); TTL finito 0 < ttl ≤ 3600. Lee el docstring de `src/delega/mem.py`. Con eso fallan 8 pruebas de `tests/test_tick_daemon.py`.

## Terminado cuando
- Las 8 pruebas de `test_tick_daemon.py` pasan **adaptando el daemon** (`src/delega/queue.py`, clase `MemClient` y el bucle) a la interfaz nueva, no relajando `mem.py`: el cliente pasa siempre org y owner; al lanzar usa `set --desde pendiente` (si rc 9, otra máquina ya la lanzó: no lanzar); rc 10 se reintenta hasta 3 veces con espera corta; las llamadas locales usan `db_path(..., org=)`.
- Ajusta las pruebas solo donde la interfaz cambió a propósito (p. ej. que ahora pasen `--org`), nunca para que dejen de comprobar lo que comprobaban.
- Añade una prueba: dos daemons que ven la misma tarea `pendiente` → solo uno la pasa a `lanzada` gracias a `--desde`.
- `cd delega && python3 -m unittest discover -s tests` en verde (antes: 308 pruebas, 8 fallos).
- Commitea en `claude/orq-int-f2`. Sin push ni merge.

## Zona
- `delega/src/delega/queue.py`, `delega/tests/test_tick_daemon.py`. **No tocar** `mem.py` ni nada más.

## Tope
- ≤ 40 min.

## Informe
≤12 líneas: qué cambiaste, sha, pruebas (números).
