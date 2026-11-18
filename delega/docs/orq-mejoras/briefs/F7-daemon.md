# F7 daemon: tareas que no se quedan colgadas ni se lanzan dos veces

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (todo integrado, 351 pruebas OK en en y es). Arreglas hallazgos de la revisión final R2 (Opus). Lee el código citado antes de cambiarlo.

## Hallazgos
2. **Bloqueante** `queue.py:528,539`: `reserve` pone `lanzada` sin `job_id`/`rama`/`nodo` y nada reconcilia `lanzada → ok/rojo`; además el daemon recorre todos los flujos `activo`, también los de `wave`, y lanza todas las olas a la vez sin `--par`. Arreglo: (a) al lanzar, guardar `job_id`, `rama` y `nodo` en la tarea; (b) reconciliar en cada ciclo las `lanzada` con el estado de su trabajo (`ok`/`rojo`) con CAS; (c) para los flujos con olas (alguna tarea con `ola > 0`), **no** lanzar el daemon por su cuenta: llamar a `wave.step` (función de `src/delega/wave.py`; mira su firma) con la org y el owner del daemon.
3. **Bloqueante** `queue.py:518`: el brief se escribe en `state_dir/queue/<task_id>.md`, común a todas las orgs; con dos daemons en el Mac el de Trycore puede sobrescribir el `t1.md` del personal entre escribir y lanzar. Arreglo: `state_dir/queue/<org>/<flujo>-<id>.md` (y el `.log` igual).
4. **Importante** `migrate_legacy` copia los items `pending` de `queue.jsonl` a la base pero los deja `pending` en el fichero: un `delega tick` normal los vuelve a lanzar. Arreglo: marcarlos `migrated` en la misma pasada bajo el lock, y que el tick normal ignore `migrated`.
9. (parte del daemon) `queue.py:206`: no comprueba que la org del perfil de la tarea coincida con `client.org` en tareas sin `profile`. Arreglo: resolver el perfil (por `repo_match`) y rechazar si su org no es la de la base.
12. (parte del daemon) el escalado a Claude se dispara por tarea sin límite: el daemon llama a `relevo` como mucho **una** vez por ciclo y por org (el resto, al ciclo siguiente).

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo; para los que R2 marcó como «prueba que no puede fallar», la prueba usa el camino real (no el doble).
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y lo mismo con `DELEGA_LOCALE=es`, los dos en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/queue.py`, `delega/tests/test_tick_daemon.py`, claves `tick.` en i18n. **No tocar**: `wave.py`, `mem.py`, `relevo.py`, `integrate.py`, `launch.py`.
- Rama `claude/orq-f7`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo (número), sha, pruebas (números), qué quedó sin verificar.
