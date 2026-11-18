# T5 limpieza: `delega clean` para los trabajos viejos de los nodos

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras`.

## Contexto
trycorepc acumula 465 carpetas de trabajos y dev-trycore 203; dev-trycore quedó bloqueado por disco (13 GB libres < 15).

## Terminado cuando
- `delega clean [--node n] [--older-than 3d] [--yes]`: sin `--yes` solo lista (trabajo, nodo, tamaño, edad); con `--yes` borra en los nodos host los directorios de trabajos con `END` cuya rama ya se trajo al Mac. Nunca toca un trabajo vivo (lease fresco) ni uno sin rama traída.
- `tests/test_clean.py` (nuevo) con el `ssh` falso: sin `--yes` no borra; con `--yes` solo los cerrados y traídos; un lease fresco no aparece.
- Una línea en `__main__.py`; suite en verde.

## Zona
- `delega/src/delega/clean.py` (nuevo), `delega/tests/test_clean.py` (nuevo), una línea en `__main__.py`, claves `clean.` en i18n.
- **No tocar** nada más. Rama `claude/orq-limpieza`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ 4 USD · 200 turnos.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste ejecutando (con números), qué quedó sin verificar y qué recomiendas.
