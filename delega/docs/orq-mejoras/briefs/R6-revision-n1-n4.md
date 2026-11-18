# R6 revisión de N1–N4 y de los arreglos del piloto de relevo

Repo `dotfiles-ia`, carpeta `delega/`. Revisas, **no** cambias código.

## Contexto
Ayer se fusionó a `main` la memoria central por org, el daemon con lease, `wave` y `relevo`. Hoy un piloto real en la org Trycore (dos nodos que se disputan la lease, `kill -9` al dueño) encontró fallos que las pruebas con dobles no veían; los arreglos están en `main` (commits `005b81c`..`5ec72f8`). Después se hicieron, en paralelo y luego juntos, N2 (la tarea guarda su `lanzador`; el nuevo dueño pregunta `delega job-state` por ssh), N4 (`sync --with-orchestrator` empuja la rama destino al bare de cada nodo) y N13 (`wave.step` con base remota, flujos con `deps` tratados como olas, `branch_prefix` del perfil). Todo eso es el diff `git diff 5ec72f8..HEAD -- delega/` de esta rama. Lee primero el docstring de `src/delega/mem.py`, y `src/delega/queue.py`, `wave.py`, `jobstate.py`, `sync.py`.

## Terminado cuando
- Has buscado, en el diff y en cómo encaja con lo que ya había: (1) caminos en los que dos dueños lancen la misma tarea o integren dos veces; (2) escrituras sin `--owner`/`--desde`/valla de época, sobre todo en la ruta remota nueva de `wave`; (3) estados a los que una tarea llega y de los que nadie la saca (p. ej. `ok` sin integrar, `lanzada` con lanzador caído tras reiniciar el daemon, `bloqueada`); (4) costuras entre N2, N4 y N13 escritas en paralelo (firmas, tuplas, claves i18n borradas que otro use); (5) supuestos de entorno (PATH sin login, HOME, macOS sin `/proc`, `ws` ausente en nodos, nodos sin credenciales de GitHub); (6) pruebas que no pueden fallar (mocks que tapan la función real).
- Para cada hallazgo das fichero:línea, el escenario concreto que lo dispara y la gravedad (bloqueante / importante / menor). Si puedes, lo demuestras con una prueba o un script en `/tmp` (no lo commitees).
- Corres `cd delega && python3 -m unittest discover -s tests` y das el número.
- Cierras con un veredicto explícito: «apto para fusionar a main: sí/no» y, si es no, la lista mínima de arreglos.
- Criterio de parada: si en 40 min no has terminado, entrega lo que tengas con el veredicto provisional.

## Zona
- Solo lectura en el repo. Nada de commits, push ni merge.

## Tope
- ≤ 45 min · ≤ 6 USD. Sin subagentes.

## Informe
Termina con un informe de ≤25 líneas: hallazgos ordenados por gravedad, número de pruebas y veredicto.
