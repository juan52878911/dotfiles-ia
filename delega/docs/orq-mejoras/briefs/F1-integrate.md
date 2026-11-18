# F1 arreglos de R1 en `integrate --merge`

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (ola 0 integrada).

## Hallazgos de la revisión R1 (Opus) que arreglas
1. **Bloqueante** `integrate.py:121-122`: TOCTOU. Si `into` avanza mientras corre `verify` (hasta 30 min), `commit-tree -p into` usa el `into` nuevo y `old` también es el nuevo, así que `update-ref` con comparación no protege; el árbol fusionado sale del `into` viejo y borra en silencio los commits nuevos. En la rama `cur == into` (l.114), `git merge` fusiona una cabeza no verificada. Arreglo: fijar el sha de `into` **antes** del ensayo y usarlo como `-p` y como `old`; si cambió, abortar con rc 8 («destino movido»).
2. **Importante** `integrate.py:96,105-107`: si el push va bien y falla `worktree add`, la ref `delega-integrate/*` queda en el bare; si el ssh de limpieza falla, nadie lo reintenta. Arreglo: un `try/finally` desde el push, limpieza con `ssh_retry` y aviso en `audit` si no se limpia.
3. **Importante** `integrate.py:191`: `--merge` en un proyecto sin `verify` fusiona sin verificar nada. Arreglo: rc 7 salvo `--allow-unverified`.
4. **Menor** `integrate.py:122`: si `into` está abierta en otro worktree, `update-ref` la mueve por debajo. Arreglo: detectarlo (`git worktree list --porcelain`) y negarse con un mensaje claro. Y que el worktree remoto lleve un sufijo único (no solo `integrate-<sha12>`) para dos integraciones a la vez.

## Terminado cuando
- Una prueba por hallazgo en `delega/tests/test_ops_integrate_sync.py` que falla sin el arreglo (para el 1: mover `into` durante un `verify` falso lento y comprobar rc 8 y destino intacto).
- `cd delega && python3 -m unittest discover -s tests` en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/integrate.py`, `delega/tests/test_ops_integrate_sync.py`, claves `integrate.` en i18n.
- **No tocar** nada más. Rama `claude/orq-f1`; commitea; sin push ni merge.

## Tope
- ≤ 45 min · ≤ 4 USD · 200 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste por hallazgo, rama y sha, pruebas y resultado (números), qué quedó sin verificar.
