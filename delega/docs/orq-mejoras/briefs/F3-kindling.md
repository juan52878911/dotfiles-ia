# F3 arreglos de R1 en kindling y gc

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (con T3 integrado).

## Hallazgos de la revisión R1 (Opus) que arreglas
1. **Importante** `jobs.py:554` y `kindling.py:118`: `gc --yes` borra las VMs «unknown», que incluyen trabajos vivos de otro orquestador u otro `state_dir` en el mismo host (personal y Trycore comparten kling-ascci). Arreglo: borrar solo si la etiqueta `delega.owner` de la VM es la de este orquestador; las demás, listarlas y dejarlas.
2. **Menor** `kindling.py:123`: las VMs en `starting`/`creating` cuentan como huérfanas «frozen», así que dos lanzamientos a la vez superan `max_jobs`. Arreglo: contar los estados transitorios como activos.

## Terminado cuando
- Una prueba por hallazgo (`tests/test_kindling.py`, con `tests/fakes/kling-bin/kling`) que falla sin el arreglo.
- `cd delega && python3 -m unittest discover -s tests` en verde.

## Zona
- `delega/src/delega/kindling.py`, `delega/src/delega/jobs.py` (solo `gc`), `delega/tests/test_kindling.py`, `delega/tests/fakes/kling-bin/kling`, claves `kindling.` en i18n.
- **No tocar** nada más. Rama `claude/orq-f3`; commitea; sin push ni merge.

## Tope
- ≤ 30 min · ≤ 4 USD · 150 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo, sha, pruebas (números), qué quedó sin verificar.
