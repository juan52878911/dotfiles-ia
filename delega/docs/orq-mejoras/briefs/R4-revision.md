# R4 revisión de la ronda 3, solo lectura

Repo `dotfiles-ia`, carpeta `delega/`. Revisa `git diff 392c81e..HEAD -- delega/`: F11 (valla de época en `mem`, `promote` con cerrojo, `unfence`), F10 (costura daemon ↔ wave ↔ relevo: `job_state` de 3 valores, `Env` con el checkout y la org del flujo, escalado por `relevo.escalate`, `FETCHED=` en meta, `integrate --sha`) y arreglos de macOS (`mem.mounts` con caché, pruebas sin `ws`).

## Qué comprobar
- Que los 3 bloqueantes de R3 están cerrados (el daemon cierra las tareas `lanzada`; lanza desde el checkout del flujo; la época frena escrituras del dueño vallado).
- Que la prueba de ciclo completo del daemon usa de verdad `wave.step`, `job_state` y `queue` reales.
- Un escritor **sin lease** (p. ej. `wave step` por cron con owner `host:pid`) no está vallado tras un `promote` (lo dijo F11): ¿es un riesgo real de doble lanzamiento con la base promovida? Propón la regla mínima.
- Regresiones nuevas; pruebas que no pueden fallar.

## Terminado cuando
- Has leído el diff y escrito el informe. No cambies código ni hagas commits.

## Tope
- ≤ 20 min · ≤ 4 USD · 120 turnos. Sin subagentes.

## Informe
Hallazgos clasificados con fichero:línea, caso y arreglo. Última línea: «apto para fusionar a main: sí/no».
