# R3 revisión de la ronda 2 de arreglos, solo lectura

Repo `dotfiles-ia`, carpeta `delega/`. Revisa `git diff 0767d87..HEAD -- delega/`: arreglos de los hallazgos de R2 (F6 `wave`, F7 daemon, F8 `integrate`/escalado/`clean`, F9 `promote`/época/etiquetas de VM) y dos arreglos del orquestador para macOS (`_pid_alive` en `wave.py`, prueba del daemon sin `mount`).

## Qué comprobar
- Que cada bloqueante de R2 está cerrado de verdad: (1) `wave step` real lanza con un fichero de brief; (2) el daemon guarda job/rama/nodo, reconcilia `lanzada` y deja las olas a `wave.step`; (3) los briefs van por org (`queue/<org>/…`, `wave/<org>/…`) y no pueden cruzarse.
- Que el Claude de escalado no puede hacer push ni merge (modo plan, herramientas de solo lectura, sin credenciales en el entorno) y su coste cuenta.
- Que la época de `promote` impide que el dueño anterior siga escribiendo.
- Regresiones nuevas que hayan introducido estos arreglos y pruebas que no pueden fallar.

## Terminado cuando
- Has leído el diff y escrito el informe. No cambies código ni hagas commits.

## Tope
- ≤ 25 min · ≤ 5 USD · 150 turnos. Sin subagentes.

## Informe
Hallazgos clasificados (bloqueante / importante / menor) con fichero:línea, caso y arreglo en una línea. Última línea: «apto para fusionar a main: sí/no».
