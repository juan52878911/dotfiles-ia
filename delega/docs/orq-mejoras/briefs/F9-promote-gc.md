# F9 promote, transiciones y etiquetas de dueño en kindling

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (todo integrado, 351 pruebas OK en en y es). Arreglas hallazgos de la revisión final R2 (Opus). Lee el código citado antes de cambiarlo.

## Hallazgos
15. **Importante** `mem.py:1059` `promote`: la copia suele ir por detrás, así que las tareas lanzadas tras la copia vuelven a `pendiente` y se relanzan; además borra `-wal`/`-shm` con conexiones abiertas y no hay época que impida al dueño anterior seguir escribiendo. Arreglo: al promover, pasar a `bloqueada` (+ evento `necesita_decision`) las tareas `lanzada`/`pendiente` con `actualizado` posterior a la fecha de la copia; guardar una **época** (`PRAGMA user_version` no; tabla `meta` con `epoca`) que `promote` incrementa y que `claim` compara: un dueño con época vieja recibe rc 9; no borrar `-wal`/`-shm` (usar la API de backup de sqlite3 para escribir la copia promovida).
19. (parte de mem) admitir como transiciones válidas `ok → arreglo` y `ok → bloqueada` (integración roja o en conflicto después de un job ok), siempre con `--desde`.
16. **Importante** `jobs.py:41` `owner_label()` vale `DELEGA_OWNER` o el nombre del cwd: no identifica al orquestador. Arreglo: etiqueta `delega.orch=<hostname>-<hash8 de state_dir>` en las VMs nuevas (`kindling.py`), `gc --yes` solo borra las que tienen la nuestra, y las VMs vivas de otro orquestador **cuentan** para `max_jobs`.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo; para los que R2 marcó como «prueba que no puede fallar», la prueba usa el camino real (no el doble).
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y lo mismo con `DELEGA_LOCALE=es`, los dos en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/mem.py`, `delega/src/delega/jobs.py` (solo `owner_label`/`gc`), `delega/src/delega/kindling.py` (etiqueta y cuenta de `max_jobs`), `delega/tests/test_mem.py`, `delega/tests/test_kindling.py`, `delega/tests/fakes/kling-bin/kling`, claves `mem.`/`kindling.` en i18n. **No tocar**: `wave.py`, `queue.py`, `relevo.py`, `integrate.py`, `clean.py`, `launch.py`.
- Rama `claude/orq-f9`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ - USD · - turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo (número), sha, pruebas (números), qué quedó sin verificar.
