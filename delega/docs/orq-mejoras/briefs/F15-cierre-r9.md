# F15 cierre de R9: el Claude escalado sin ssh, y E2–E4 acotados

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-e` (`9d3e4da`). Un solo trabajo.

## Contexto
R9 revisó E1–E4 (escalado y decisiones del relevo del orquestador) y dijo «no apto»:
1. **E1 (grave)**: al Claude escalado se le permite `Bash(ssh <host> .local/bin/delega mem *)` (`relevo.mem_access`, `relevo.py` ~281-296). ssh concatena los argumentos y el shell remoto los reinterpreta: `ssh h .local/bin/delega mem export ';' git push …` o `--error '$(…)'` casa la regla en local y ejecuta lo que sea en el host de la base. Además la `--owner` común añadida a `mem.main` (`mem.py` ~1462) deja escribir por ssh con cualquier dueño: la valla de época solo frena a dueños que ya están en `owner_epochs` (~412).
2. **E3**: al tercer rechazo de lanzamiento (`wave.py` ~650) solo se emite un evento; la tarea sigue `ok` y el ciclo integrar → rojo → arreglo → rechazo → `ok` se repite sin fin.
3. **E4**: un evento `lanzador_sin_respuesta` por ciclo incluso pasado el tope, sin límite (`queue.py` ~690); `_fallos` (~671) hace un `export` completo de la base (por ssh si es remota) por ciclo y por tarea ajena, también cuando el lanzador responde (~736).
4. **E2**: `log_tail` (`wave.py` ~339) copia `stderr.log` al `registro` sin pasar por `broker.redact` (`broker.py` l. 90) con los términos prohibidos del perfil.
Lee `relevo.py` (`mem_access`, `claude_argv`, `spawn`, `brief_for`), `queue.py` (`MemClient._ssh`, `task_state`, `_fallos`), `wave.py` (paso de arreglos), `mem.py` (`main`, valla de época) y `broker.redact`.

## Terminado cuando
- **E1**: el Claude escalado **no recibe ssh**. Orden local nueva `delega relevo-mem <orden> [args]` (módulo nuevo o en `relevo.py`) que corre en el host del daemon y:
  - solo admite `log`, `export` (sin `--out`), `next`, `set` y `event`; rechaza (rc 2) cualquier otra (`promote`, `unfence`, `claim`, `init`, `backup`…) y cualquier `--db`/`--org`/`--owner` que traiga el Claude: los fija ella desde la config del daemon (`cfg.memoria_for(org)`);
  - habla con la base a través de `MemClient` (local o `_ssh`, que ya cita cada argumento con `shlex.quote`); nunca construye una línea de shell con texto del Claude sin citar;
  - escribe con el **dueño del daemon que tiene la lease** (pasado por el entorno al lanzar al Claude, p. ej. `DELEGA_RELEVO_OWNER`), de modo que si ese dueño fue vallado por un `promote` la escritura falla (rc 9).
  - El brief del Claude usa `delega relevo-mem …` y `--allowedTools` permite solo `Bash(delega relevo-mem *)` (y `Read`, `Grep`, `Glob`); fuera `ssh`, `git`, `gh`, `Edit`, `Write`.
  - Quita la `--owner` común de `mem.main` (vuelve a como estaba antes de E1) salvo que algo más la necesite; si la necesita, explícalo.
- **E3**: al tercer rechazo seguido la tarea pasa a `bloqueada` (CAS desde `ok`/`arreglo`) además de pedir la decisión una vez; no hay más reintentos.
- **E4**: no se emiten más eventos `lanzador_sin_respuesta` una vez alcanzado el tope para esa tarea; el recuento sale de una consulta acotada (eventos de esa tarea desde el último `lanzador_responde`, p. ej. `mem log --flow` con límite o una orden de lectura filtrada) y no de un `export` completo; si el lanzador responde, no se consulta nada.
- **E2**: `registro` pasa por `broker.redact` con los `forbidden_terms` del perfil de la tarea (y los secretos que ya redacta el broker) antes de guardarse.
- Pruebas en `tests/test_f15_cierre.py` sin simular `relevo-mem`, `MemClient`, `task_state` ni `wave.step` (sí ssh por host y `claude`/`delega run` falsos): `relevo-mem export '; touch /tmp/pwn'` y `--error '$(touch …)'` a través del ssh falso **no crean el fichero**; `relevo-mem promote` y `--db x` → rc 2; dueño vallado por un `promote` → `relevo-mem set` rc 9; el argv del Claude no contiene `ssh`; tercer rechazo → `bloqueada` y ningún lanzamiento más; 25 ciclos sin respuesta → exactamente `LANZADOR_MAX_FALLOS` eventos y una decisión, sin `export`; `registro` con un término prohibido sale redactado.
- `cd delega && python3 -m unittest discover -s tests` en verde en `en` **y en `es` (suite completa)**. Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/relevo.py`, `queue.py`, `wave.py`, `mem.py` (solo quitar la `--owner` común o lo mínimo de E4), un módulo nuevo para `relevo-mem` y su registro en `__main__.py`, `delega/src/delega/i18n/*.json`, `delega/tests/test_f15_cierre.py` y pruebas existentes que haya que adaptar (incluidas las de E).
- **No tocar**: `launch.py`, `integrate.py`, `sync.py`, `kindling.py`, `broker.py` (solo usar `redact`).
- Rama `claude/orq-f15`; commitea; sin push ni merge.

## Tope
- ≤ 75 min · ≤ 8 USD · 500 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: cómo cerraste 1–4, rama y sha, pruebas en en y es (números), qué quedó sin verificar.
