# R8 confirmación: ¿cierra F14 los tres parciales de R7?

Repo `dotfiles-ia`, carpeta `delega/`. Revisas, **no** cambias código.

## Contexto
R7 dejó tres parciales en el relevo del orquestador: (a) tarea `pend:` del Mac sin `lanzador` → el nodo que toma el relevo juzga el pid local y la relanza; (b) se pregunta por ssh al **nombre** del nodo y no a su alias `cfg.node(n)["ssh"]`; (c) `sync --with-orchestrator` empujaba la rama destino con `+` y retrocedía el bare. F14 (`543d3d0..819101e`) dice cerrarlos: el Mac graba `socket.gethostname()` como `lanzador`, `task_state` nunca juzga un pid ajeno y pide `necesita_decision` motivo `lanzador_sin_ssh` si no hay ssh posible, alias traducido, push sin `+` (aviso `sync.branch_ahead`). Mira `git diff 543d3d0..HEAD -- delega/` y `tests/test_f14_cierre.py`.

## Terminado cuando
- Para (a), (b) y (c): «cerrado / no cerrado» con fichero:línea y, si no, el escenario.
- Buscas regresiones del diff: que el Mac con un hostname que coincida con un nombre de nodo se confunda con él; tareas `lanzada` que ahora se queden para siempre sin decisión; que `sync.branch_ahead` devuelva éxito cuando el nodo tiene una rama que no contiene `origin/<rama>` (divergencia) y nadie lo sepa; claves i18n.
- Corres `cd delega && python3 -m unittest discover -s tests` y das el número.
- Veredicto «apto para fusionar a main: sí/no»; si es no, la lista mínima.
- Criterio de parada: si en 20 min no has terminado, entrega lo que tengas con veredicto provisional.

## Zona
- Solo lectura. Nada de commits, push ni merge.

## Tope
- ≤ 25 min · ≤ 3 USD. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: estado de (a), (b), (c), regresiones, número de pruebas y veredicto.
