# N4 la rama destino de cada flujo llega a los nodos

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` (con los arreglos del piloto de relevo del 18-nov).

## Contexto
En el piloto, los bares de los nodos (`~/<workdir>/<repo>.git`) los crea `delega` con `git init --bare` y solo reciben las ramas de trabajo que empuja el Mac: no tenían `main` y `relevo.origin` (`src/delega/relevo.py`) no encontraba base. Los nodos no tienen credenciales de GitHub, así que no pueden traerla solos: la tuve que empujar a mano desde el checkout del Mac. Lee `src/delega/sync.py`, `relevo.origin` y `relevo.install_on_node`, y el docstring de `src/delega/mem.py`.

## Terminado cuando
- `delega sync --profile <p> --with-orchestrator [nodo…]` además empuja a cada nodo, para cada flujo **activo** de la org del perfil cuyo repo tenga checkout en el Mac (`[memoria.<org>].repos` o el checkout que resuelva `queue._checkout`), la rama destino del flujo (`rama_destino`, o `main` si no tiene) desde `refs/remotes/origin/<rama>` (tras `git fetch origin <rama>`; si no hay `origin/<rama>`, la rama local) a `refs/heads/<rama>` del bare del nodo (`git push <alias>:<workdir>/<repo>.git +<ref>:refs/heads/<rama>`), crea el bare con `git init --bare` si falta y deja su `HEAD` apuntando a esa rama si el `HEAD` del bare no resuelve.
- Nunca empuja a GitHub ni a otro remoto que no sea el bare del nodo; nunca toca ramas que no sean la destino. `--dry-run` lista lo que empujaría.
- Un flujo cuyo repo no tiene checkout en el Mac se salta con un aviso (no es error).
- Pruebas en `tests/test_n4_sync_ramas.py` con un nodo falso (alias ssh → directorio local, como en las pruebas de sync existentes): bare sin `main` → tras sync tiene `main` = `origin/main` del checkout y `HEAD` resuelve; flujo inactivo no se empuja; `--dry-run` no escribe; flujo de otra org no se empuja.
- Mensajes: usa las claves i18n que ya existen (`sync.*`, `relevo.*`); **no** añadas claves (otro trabajo tiene los `i18n/*.json`). Si te hace falta una, ponla en el informe con su texto en/es y usa mientras un `print` sin traducir marcado `# TODO i18n`.
- `cd delega && python3 -m unittest discover -s tests` en verde. Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/sync.py`, `delega/src/delega/relevo.py` (solo lo que sync necesite; `origin` no cambia de firma), `delega/tests/test_n4_sync_ramas.py`.
- **No tocar**: `queue.py`, `wave.py`, `mem.py`, `launch.py`, `i18n/*.json`.
- Rama `claude/orq-n4`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ 5 USD · 400 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤12 líneas: qué cambiaste, rama y sha, pruebas y resultado (números), qué quedó sin verificar.
