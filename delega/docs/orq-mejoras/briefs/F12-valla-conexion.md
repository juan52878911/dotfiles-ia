# F12 valla por época de conexión en `delega mem` (hallazgos de R4)

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (396 pruebas OK en en y es).

## Hallazgos
- **A1 (alto)** `mem.py:1310-1320`: `promote` hace `COMMIT` (l.1312) antes de `promoted.backup(destination)` (l.1320). Un escritor sin lease (`wave step` por cron con owner `host:pid`, sin fila en `owner_epochs`) pasa `_check_fence` (l.386), hace `pendiente→lanzada` en la base vieja y lanza; la copia lo pisa y el daemon nuevo la relanza: doble lanzamiento. **Regla**: cada conexión guarda la `epoca` que lee al abrirse (`connect`/`migrate`); `write()` (todas las escrituras) devuelve `MemConflict` (rc 9) si la `epoca` actual de la base no es la de la conexión, tenga o no lease el escritor. Además, `promote` no suelta el cerrojo hasta después de copiar la base promovida (o, si la API de backup de sqlite lo impide, incrementa la época **antes** del `COMMIT` en la base principal para que cualquier escritor con la época vieja quede fuera).
- **M1 (medio)** `tests/test_mem.py` `test_promote_holds_the_write_lock_of_the_main_database`: espía `mem.backup`, que solo se usa para `.before-promote`. Añade una prueba con un escritor sin lease (owner `host:pid`, conexión abierta antes del `promote`) que intenta escribir entre el `COMMIT` y la copia final (o justo después del `promote`): hoy escribe; con A1 recibe rc 9 y no escribe.
- **M2 (medio)** `wave.py:377` `_event` llama a `mem.write(... mem._event ...)` sin valla. Arreglo: `mem.add_event(..., quien=mem.owner_id())` (y la regla A1 lo cubre).
- **B1 (bajo)** `mem.py:1233-1239`: si `BEGIN IMMEDIATE` falla por algo que no sea «ocupado», `promote` sigue sin cerrojo. Arreglo: relanzar siempre.
- **B2 (bajo)** `add_task`, `add_flow`, `init` sin valla: cubierto por A1 en `write()`; añade una prueba de `add_task` con conexión de época vieja.
- **B3 (bajo)** `queue.py` `_reconcile_tasks`: `lanzada` con job_id real pero sin directorio (limpiado u otro `state_dir`) pasa a `rojo`. Arreglo: dejarla como está y emitir `necesita_decision` (una vez) en vez de `rojo`.

## Reglas de ejecución (importante)
- Pruebas **siempre en primer plano** (Bash normal, timeout hasta 600000 ms). Nunca en segundo plano, Monitor ni esperando notificaciones.
- **Commitea en cuanto tus pruebas nuevas pasen**, antes de la suite completa.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo.
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y con `es`, los dos en verde.

## Zona
- `delega/src/delega/mem.py`, `delega/tests/test_mem.py`, en `delega/src/delega/wave.py` solo `_event`, en `delega/src/delega/queue.py` solo `_reconcile_tasks`, sus pruebas, claves `mem.` en i18n.
- Rama `claude/orq-f12`; commitea; sin push ni merge.

## Tope
- ≤ 40 min · ≤ 5 USD · 250 turnos. Sin subagentes.

## Informe
≤12 líneas: qué cambiaste por hallazgo, sha, pruebas (números).
