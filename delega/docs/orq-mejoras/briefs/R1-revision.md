# R1 revisión: la ola 0 integrada en `claude/orq-mejoras`, solo lectura

Repo `dotfiles-ia`, carpeta `delega/`. Revisa `git diff 32048d0..HEAD -- delega/` (main → ola 0: fusión fase 1 de kling, estabilización de pruebas, `integrate --merge --node`, `delega mem`).

## Qué buscar
- Caminos donde un trabajo quede abierto sin `END`, una VM sin dueño o una lease que nadie libere.
- En `integrate --merge`: que un `verify` rojo o un conflicto nunca dejen tocada la rama destino; limpieza del worktree temporal y de la ref temporal en el nodo también si el ssh se corta a mitad.
- En `mem.py`: transacciones que puedan exceder `busy_timeout`, condiciones de carrera en `claim`/`heartbeat`, validación de org y de repo remoto, que una base personal y una de Trycore no puedan mezclarse.
- Pruebas que pasan sin poder fallar.

## Terminado cuando
- Has leído el diff completo y escrito el informe. No cambies código ni hagas commits.

## Tope
- ≤ 30 min · ≤ 6 USD · 150 turnos. Sin subagentes.

## Informe
Hallazgos clasificados (bloqueante / importante / menor), cada uno con fichero:línea, el caso concreto que falla y el arreglo propuesto en una línea. Máx. 25 líneas.
