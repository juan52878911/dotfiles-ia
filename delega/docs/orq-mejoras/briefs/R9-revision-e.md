# R9 revisión de E1–E4 (escalado y decisiones del relevo)

Repo `dotfiles-ia`, carpeta `delega/`. Revisas, **no** cambias código.

## Contexto
En un piloto real (org Trycore, base en otro host) la decisión que pidió el relevo no se pudo tomar. E1–E4 (`git diff 57956c0..HEAD -- delega/`) dicen arreglarlo: E1 el Claude escalado recibe el comando exacto a la base de la org (`ssh <host> .local/bin/delega mem … --db … --org … --owner …`) y solo ese `Bash(...)` permitido, más una opción `--owner` común en `mem.main`; E2 todo `necesita_decision` lleva `registro`; E3 un arreglo rechazado al lanzar (rama abierta en otro worktree, rc 2) vuelve a `ok` sin sumar intento y a los 3 rechazos pide decisión; E4 el contador de lanzador sin respuesta vive en la base (eventos `lanzador_sin_respuesta`/`lanzador_responde`). E1 está probado con un `claude` real contra el host real.

## Terminado cuando
- Para E1–E4: «cerrado / no cerrado» con fichero:línea y, si no, el escenario.
- Buscas: (1) si la `--owner` común de `mem.main` permite a cualquiera escribir saltándose la valla de época o hacerse pasar por el dueño de la lease; (2) si el `Bash(ssh <host> .local/bin/delega mem *)` deja ejecutar algo más que `mem` (`;`, `&&`, `$(…)`, redirecciones en el resto de la línea) y qué órdenes de `mem` puede usar el Claude (¿`promote`, `unfence`?); (3) si E3 puede reintentar sin fin o integrar dos veces; (4) si los eventos de E4 hacen crecer la base sin límite o rompen `export`/`candidates` (coste por ciclo); (5) secretos o datos de otra org en `registro`; (6) pruebas que no pueden fallar.
- Corres `cd delega && python3 -m unittest discover -s tests` y das el número.
- Veredicto «apto para fusionar a main: sí/no»; si es no, la lista mínima.
- Criterio de parada: si en 25 min no has terminado, entrega lo que tengas con veredicto provisional.

## Zona
- Solo lectura. Nada de commits, push ni merge.

## Tope
- ≤ 30 min · ≤ 4 USD. Sin subagentes.

## Informe
Termina con un informe de ≤20 líneas: estado de E1–E4, hallazgos por gravedad, número de pruebas y veredicto.
