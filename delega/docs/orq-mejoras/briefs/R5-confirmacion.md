# R5 confirmación: ¿apto para fusionar a main?

Repo `dotfiles-ia`, carpeta `delega/`. Revisa `git diff b2b651e..HEAD -- delega/src/delega/mem.py delega/src/delega/wave.py delega/src/delega/queue.py delega/tests/test_mem.py delega/tests/test_tick_daemon.py` (F12: valla por época de conexión en `write()`, `promote` con la época subida antes del `COMMIT`, `wave._event` por `add_event`, `BEGIN IMMEDIATE` sin tragar errores, `lanzada` sin directorio → `necesita_decision`).

## Qué comprobar
- Que A1 de R4 queda cerrado: ninguna conexión abierta con la época vieja puede escribir en la base después de que `promote` suba la época, con o sin lease.
- Que la regla no rompe el uso normal: un daemon que abre conexión nueva en cada ciclo sigue funcionando tras un `promote` legítimo; ninguna escritura propia de `promote` queda vallada.
- Regresiones o pruebas que no pueden fallar.

## Terminado cuando
- Has leído el diff y escrito el informe. No cambies código ni hagas commits.

## Tope
- ≤ 15 min · ≤ 3 USD · 80 turnos. Sin subagentes.

## Informe
≤12 líneas. Última línea: «apto para fusionar a main: sí/no».
