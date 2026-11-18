# T4 rama en uso: relanzar sobre la rama de un trabajo cerrado sin pelearse con delega

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras`.

## Contexto
Se gastaron 420,8 USD en vueltas repetidas sobre la misma rama y muchos relanzamientos chocan con el worktree de un trabajo ya terminado.

## Terminado cuando
- Al lanzar con `--branch <r>` cuyo worktree pertenece a un trabajo con `END`, delega retira ese worktree y sigue; si el trabajo sigue vivo, rechaza con rc 5 como hoy.
- Al traer la rama de vuelta: si la rama local está abierta en un worktree (git rechaza «refusing to fetch into branch … checked out»), traerla a `refs/delega/<id>` y hacer `merge --ff-only` en ese worktree si está limpio; si no, avisar con la ref. Hoy el trabajo queda ok pero su commit solo está en `out.bundle`.
- Aviso si HEAD local va por detrás de `origin/<rama>`.
- Aviso (lint) a partir de la **tercera** vuelta sobre la misma rama en 24 h, con el coste acumulado.
- Arreglos de la revisión R1:
  - `launch.py:379`: `rc = None` justo antes de `given, rc = rc, None` deja `given` siempre en None; `recover` ignora el rc leído y `recover --force` con lease viva espera en vez de cerrar. Quita esa línea y añade la prueba de `--force` sin fichero `rc`.
  - `jobs.py:514`: `recover` sobrescribe `waiter.pid` sin mirar si hay un `adopt` vivo (doble END/scan/destroy). Rechaza si ese pid vive, salvo `--force`.
- Pruebas nuevas en `tests/test_launch.py` y `tests/test_lint.py` que fallan sin el cambio; suite en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/launch.py` (fuera del parser de opciones, que toca T3), `delega/src/delega/jobs.py` (solo `recover`), `delega/src/delega/remote.py`, `delega/src/delega/lint.py`, sus pruebas, claves `launch.`/`lint.` en i18n.
- **No tocar**: `kindling.py`, `queue.py`, `mem.py`, `clean.py`.
- Rama `claude/orq-rama`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste ejecutando (con números), qué quedó sin verificar y qué recomiendas.
