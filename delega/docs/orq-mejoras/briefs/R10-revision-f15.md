# R10 revisión de F15: ¿cierra los hallazgos de R9?

Repo `dotfiles-ia`, carpeta `delega/`. Revisas, **no** cambias código.

## Contexto
R9 dijo «no apto» sobre E1–E4 (escalado del relevo): (1) el Claude escalado tenía `Bash(ssh <host> … delega mem *)` y ssh reinterpretaba `;`/`$(…)` en el host remoto, y una `--owner` común en `mem.main` saltaba la valla de época; (2) E3 reintentaba sin fin tras el tercer rechazo; (3) E4 emitía eventos sin tope y hacía un `export` completo por ciclo; (4) E2 guardaba `registro` sin redactar. F15 (`git diff 9d3e4da..HEAD -- delega/`) dice cerrarlos con `delega relevo-mem` (`src/delega/relevo_mem.py`: lista cerrada de órdenes, sin `--db/--org/--owner/--out`, `MemClient`, escritura con `DELEGA_RELEVO_OWNER`), bloqueo al tercer rechazo, `mem log --task` acotado y `broker.redact`. Ya comprobado a mano en los nodos reales: `relevo-mem log '; touch /tmp/pwn1'` y `--datos '{"x":"$(touch /tmp/pwn2)"}'` no crearon nada en el host de la base; `promote` rc 2; un `claude` real con el argv generado leyó la base y se le denegó `ssh`.

## Terminado cuando
- Para (1)–(4): «cerrado / no cerrado» con fichero:línea y, si no, el escenario.
- Buscas: que `relevo-mem` acepte opciones peligrosas por otra vía (`--db=x`, abreviaturas de argparse, `--`, variables de entorno que el Claude pueda fijar en su línea, p. ej. `DELEGA_RELEVO_OWNER=x delega relevo-mem …` casando el permiso); que `set` permita al Claude hacer algo que no debería (marcar `hecha` sin integrar, `--forzar`); que `mem log --task` sea correcta y acotada; que la redacción no rompa el JSON del evento; regresiones en daemon/wave/relevo; pruebas que no pueden fallar.
- Corres `cd delega && python3 -m unittest discover -s tests` y das el número.
- Veredicto «apto para fusionar a main: sí/no»; si es no, la lista mínima.
- Criterio de parada: si en 25 min no has terminado, entrega lo que tengas con veredicto provisional.

## Zona
- Solo lectura. Nada de commits, push ni merge.

## Tope
- ≤ 30 min · ≤ 4 USD. Sin subagentes.

## Informe
Termina con un informe de ≤20 líneas: estado de (1)–(4), hallazgos por gravedad, número de pruebas y veredicto.
