# F11 valla de época en `delega mem` y `promote` seguro (hallazgos de R3)

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras`. Solo `mem.py` y su prueba (y una nota en `jobs.py`/README de migración).

## Hallazgos
3. **Bloqueante** `mem.py:506,1028`: la época solo se mira al tomar/renovar/soltar el lease. Tras un `promote`, el dueño anterior recibe rc 9 en `claim` pero `mem set` y `mem event` le devuelven 0: un daemon a mitad de ciclo o un `wave step` por cron siguen escribiendo. Arreglo: comprobar la valla (`owner_epochs`) **dentro de la transacción** de `set_task`, `add_event` y cualquier escritura que lleve owner (rc 9 si el owner está vallado). Prueba: tras `promote`, `set` y `event` del dueño viejo dan rc 9 y no escriben.
4. **Importante** `mem.py:1214`: `promote` bloquea todas las `pendiente`/`lanzada`, no solo las posteriores a la copia. Arreglo: filtrar por `actualizado > copied_at`; prueba con una tarea anterior a la copia que debe quedar como estaba.
5. **Importante** `mem.py:1186-1250`: entre `_promoted_snapshot(main)` y `promoted.backup(destination)` no hay cerrojo: lo que escriba el dueño viejo en ese hueco se pisa. Además las tareas en `arreglo` no se revisan. Arreglo: `BEGIN IMMEDIATE` en la base principal durante toda la operación (si está accesible) e incluir `arreglo` en las tareas a revisar.
6. **Importante** `mem.py:369` `_remember_owner_epoch` con `MIN` deja vetado para siempre a todo dueño que tuvo lease: un failback al host original o restaurar una copia en la misma máquina deja al daemon legítimo con rc 9 eterno. Arreglo: guardar la época del **último** lease de cada dueño, y `mem unfence <owner> --yes` (y `promote --owner <nuevo>`) para readmitirlo; prueba de failback.
14. **Menor** `jobs.py:591`: las VMs con solo la etiqueta antigua `delega.owner` ya no las borra `gc`. Arreglo: que `gc` las **liste** como «etiqueta antigua, bórrala a mano con …» (sin borrarlas) y una línea en `ARCHITECTURE.md`.

## Reglas de ejecución (importante)
- Pruebas **siempre en primer plano** (Bash normal, timeout hasta 600000 ms). Nunca en segundo plano, Monitor ni esperando notificaciones: si terminas el turno esperando, delega cierra el trabajo y se pierde lo no commiteado.
- **Commitea en cuanto tus pruebas nuevas pasen**, antes de la suite completa; si la suite falla, arregla y otro commit.
- Las pruebas nuevas usan las funciones reales que se llaman entre módulos (no mocks con otra forma): R3 encontró un mock de 2 valores donde la función real devuelve 3.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo.
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y con `es`, los dos en verde.

## Zona
- `delega/src/delega/mem.py`, `delega/tests/test_mem.py`, en `delega/src/delega/jobs.py` solo el listado de `gc`, `delega/ARCHITECTURE.md` (una línea), claves `mem.`/`gc` en i18n. **No tocar** `queue.py`, `wave.py`, `relevo.py`, `launch.py`.
- Rama `claude/orq-f11`; commitea; sin push ni merge.

## Tope
- ≤ 50 min · ≤ 5 USD · 250 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo, sha, pruebas (números).
