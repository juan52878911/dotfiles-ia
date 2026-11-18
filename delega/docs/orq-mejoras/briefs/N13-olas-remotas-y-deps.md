# N13 cualquier dueño de la lease lleva olas y dependencias, y la rama sigue el prefijo del perfil

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` con N2 integrado (columna `lanzador` y orden `delega job-state`).

## Contexto
Piloto de relevo del 18-nov (org Trycore, base en `trycorepc`, daemons en `trycorepc` y `dev-trycore`), tres huecos:
1. **Olas solo en el host de la base** (`_wave_step` en `src/delega/queue.py` sale con `tick.wave_remote` si `client.m["local"]` es falso): si cae ese host nadie lleva las olas.
2. **Dependencias sin olas se quedan paradas**: `mem next` exige deps en `hecha`, y en un flujo sin olas (todas `ola` = 1) nadie pasa de `ok` a `hecha`. En el piloto se hizo a mano.
3. **Prefijo de rama**: el daemon usa `delega/q-<flujo>-<tarea>` (`queue.py`, `daemon_cycle`) y `wave` su propio nombre; Oriuno tiene un hook que solo admite `feature/`, `fix/`, `chore/`: el trabajo terminó con rc 0 sin commit.
Lee el docstring de `src/delega/mem.py`, `daemon_cycle`, `_wave_step`, `_flow_env` en `queue.py`, y `step`/`Env` en `src/delega/wave.py`.

## Terminado cuando
- `wave.step` funciona con la base remota: el daemon pasa un cliente (el `MemClient` que ya habla por ssh con `delega mem … --json`) en lugar de una conexión SQLite local, o `wave` usa una interfaz común con dos implementaciones (local y ssh). Todas las escrituras siguen yendo con `--owner`/`--org`/`--desde` y la valla de época. Un daemon en un nodo sin la base lleva una ola completa (lanzar, reconciliar, integrar en el clon del relevo `~/<workdir>/relevo/<repo>`, pasar a `hecha`).
- Un flujo cuyas tareas tienen `deps` se trata como flujo de olas aunque todas tengan `ola` = 1: las tareas `ok` se integran en la rama destino (`integrate --merge`) y pasan a `hecha`, y entonces sus dependientes quedan listas. Un flujo sin deps ni olas sigue como hoy.
- La rama de cada tarea usa `branch_prefix` del perfil (clave nueva opcional en `[profiles.<p>]`, p. ej. `"chore/"`; por defecto `delega/`), en el daemon y en `wave`. `branch` explícito en `run_args` sigue ganando. Documenta la clave en el README/config de ejemplo de delega (sin datos reales).
- Pruebas en `tests/test_n13_olas_remotas.py` con `ssh` falso hacia otra base local: (a) un daemon con base remota lleva una ola de 2 tareas hasta `hecha`; (b) flujo con `deps` y `ola` = 1: la segunda arranca solo tras integrar la primera; (c) `branch_prefix = "chore/"` → rama `chore/q-…`, y `branch` explícito gana; (d) sigue en verde la prueba de ciclo completo del daemon de F10 con las funciones reales.
- `cd delega && python3 -m unittest discover -s tests` en verde (en y es). Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/queue.py`, `delega/src/delega/wave.py`, `delega/src/delega/config.py` (solo `branch_prefix`), `delega/src/delega/i18n/*.json` (claves `n13.` o `wave.`/`tick.`), la documentación de config de delega, `delega/tests/test_n13_olas_remotas.py` y las pruebas existentes de `wave`/`tick` que haya que adaptar.
- **No tocar**: `mem.py` (su interfaz es estable: si te falta una orden, dilo en el informe), `launch.py`, `integrate.py`, `sync.py`, `relevo.py`.
- Rama `claude/orq-n13`; commitea; sin push ni merge.

## Tope
- ≤ 90 min · ≤ 10 USD · 600 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, pruebas y resultado (números), qué quedó sin verificar.
