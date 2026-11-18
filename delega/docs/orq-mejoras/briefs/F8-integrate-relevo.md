# F8 integrate, escalado a Claude y clean: sin fugas entre orgs ni Claude con poder de push

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (todo integrado, 351 pruebas OK en en y es). Arreglas hallazgos de la revisión final R2 (Opus). Lee el código citado antes de cambiarlo.

## Hallazgos
6. **Importante** `integrate.py:275` devuelve 8 (conflicto) cuando el destino se movió (`DestMoved`); dos olas integrando a la vez acaban `bloqueada` y lanzan Claude sin conflicto real. Arreglo: rc **11** propio para `DestMoved` (documentado en el uso).
10. **Importante** `integrate.py:82` `run_verify_remote` hace push del commit fusionado a cualquier `--node`; con un nodo personal y un repo de Trycore, el código sale del perímetro. Arreglo: rc 2 si el perfil del repo no está en `node["profiles"]`.
11. **Importante** `relevo.py:30,198`: la lista de herramientas prohibidas se esquiva (`git -C x push`, `cd r && git push`, `git update-ref`, `delega integrate --merge --into main`, `gh api …/merges`); con `ws exec` hereda credenciales y el contexto ws activo. Arreglo: lanzar el Claude de escalado con `--permission-mode plan` **y** `--allowedTools` cerrado a lectura (`Read`, `Grep`, `Glob`) más `Bash(delega mem *)`; entorno sin `GH_TOKEN`/`GITHUB_TOKEN`/`SSH_AUTH_SOCK`; `ws exec` con el contexto de la org de la base (o `claude` a secas si no se puede fijar). Prueba que la línea de órdenes generada contiene esas opciones y que el entorno no tiene esas variables.
12. (parte de relevo) cada escalado gasta hasta 3 USD fuera del presupuesto de delega. Arreglo: registrar el escalado en `audit` con su tope y sumar su coste a `spend` cuando termine (lee cómo lo hace `launch.py` para los trabajos).
13. **Importante** `relevo.py:222-232`: el evento `once` se escribe antes de `set bloqueada` y del `spawn`; si cualquiera falla, la tarea queda muerta sin reintento. Arreglo: `set` y `spawn` primero y el evento `once` al final; si el `spawn` falla, evento `escalado_fallido` y se reintenta en el ciclo siguiente.
17. **Menor** `clean` usa «`fetch.log` vacío» como señal de rama traída. Arreglo: exigir además `FETCHED=<sha>` en `meta` si existe; si el `meta` no lo tiene (trabajos viejos), mantener la regla actual. (Que `launch` escriba `FETCHED=` lo hará otro trabajo: aquí solo léelo.)
18. **Menor** `clean.py:~96`: `cat lease.json` con rc 1 se toma como «sin lease», también con permiso denegado. Arreglo: `test -e` y distinguir los casos; en la duda, no borrar.

## Reglas de ejecución (importante)
- Corre las pruebas **siempre en primer plano** (Bash normal con timeout de hasta 600000 ms). **Nunca** en segundo plano, con `run_in_background`, Monitor ni esperando notificaciones: si terminas el turno esperando, delega cierra el trabajo y se pierde lo no commiteado (le pasó al primer intento de este mismo trabajo).
- **Commitea en cuanto tus pruebas nuevas pasen**, antes de lanzar la suite completa (~230 s por idioma); si la suite completa falla, arregla y haz otro commit.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo; para los que R2 marcó como «prueba que no puede fallar», la prueba usa el camino real (no el doble).
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y lo mismo con `DELEGA_LOCALE=es`, los dos en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/integrate.py`, `delega/src/delega/relevo.py`, `delega/src/delega/clean.py`, sus pruebas (`tests/test_ops_integrate_sync.py`, `tests/test_relevo.py`, `tests/test_clean.py`), claves `integrate.`/`relevo.`/`clean.` en i18n. **No tocar**: `wave.py`, `queue.py`, `mem.py`, `launch.py`.
- Rama `claude/orq-f8`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo (número), sha, pruebas (números), qué quedó sin verificar.
