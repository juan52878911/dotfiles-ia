# F13 la costura N2 ↔ N13 ↔ relevo: sin doble lanzamiento y con lo integrado en la base de lanzamiento

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-n-int` (main + N2 + N4 + N13). Un solo trabajo para toda la costura.

## Contexto
La revisión R6 dice «no apto». N2 (la tarea guarda `lanzador`; `_reconcile_foreign` pregunta `delega job-state` por ssh) y N13 (`wave.step` con base remota; flujos con `deps` como olas) se escribieron en paralelo y no encajan. Hallazgos a cerrar:
1. **Bloqueante**: `wave.step` (paso 1, `wave.py` ~523-526, ~346, ~581; llamado desde `queue.py` ~729) solo mira `env.job_state(t)` local. Tras un relevo el nuevo dueño no tiene el directorio del trabajo de otro, lo ve `fail`, lo pasa a `rojo` y el paso 1b lo relanza como `arreglo` con el trabajo original vivo.
2. **Bloqueante**: en un nodo, `relevo.origin` (`relevo.py` ~161-167) deja el clon con HEAD suelto en la rama destino del bare en cada llamada; `integrate --merge` (`wave.py` ~630) solo mueve `refs/heads/<into>` del clon y nada lo lleva al bare; la ola siguiente (o la dependiente) parte sin lo integrado, y tras un relevo se pierde.
3. `lanzador` es `client.owner` (por defecto `<host>:tick`), y `ssh host:tick` no resuelve (`queue.py` ~618, ~812). La prueba de N2 usa un ssh falso que lo acepta.
4. Si el lanzador responde sin directorio del trabajo, la ruta foránea marca `rojo` (`queue.py` ~683, `jobstate.py`); la local en ese caso pide decisión.
5. `_reconcile_foreign` llama a `fetch_branch` con `fl["repo"]` (`acme/api` → `<workdir>/acme/api.git`); debe usar el nombre del repo (`relevo.repo_basename` o `REPO` de meta). La prueba simula `wave_have` y no pasa por ahí.
6. Menores a cerrar si caben: N4 empuja con `+` una ref vieja si el `fetch` de `origin` falla (que no empuje); `job-state` crea directorios (que no escriba nada).
Lee primero `src/delega/wave.py` (`step`, `Env`, `ClientStore`), `src/delega/queue.py` (`_reconcile_tasks`, `_reconcile_foreign`, `daemon_cycle`, `_wave_step`), `src/delega/jobstate.py`, `src/delega/relevo.py` (`origin`), `src/delega/sync.py` y el docstring de `src/delega/mem.py`. Los scripts de demostración de R6 no están disponibles: reprodúcelos tú.

## Terminado cuando
- **Una sola función** decide el estado de una tarea `lanzada` (local si el directorio está aquí; si no y el `lanzador` es otro, por ssh con `job-state`), y la usan tanto `_reconcile_tasks` como el paso 1 de `wave.step`. Un trabajo ajeno sin respuesta sigue `lanzada` (el contador de N2) y nunca se relanza; «no encontrado» en el lanzador → `necesita_decision` motivo `job_sin_directorio` (una vez), nunca `rojo`. `job-state` devuelve un estado propio para «no encontrado» y no crea ficheros.
- `wave.step` escribe `lanzador` al lanzar, igual que el daemon.
- `lanzador` es un host alcanzable por ssh: el nombre del nodo (`[relevo] node` en un nodo) o un `host` explícito del daemon (`tick --daemon --host <alias>`, por defecto el nodo); queda separado del `owner` de la lease. Documentado en el docstring del daemon. Desde un lanzador sin alias (el Mac) se pide decisión tras el contador, sin ssh.
- Lo integrado llega a la base de lanzamiento: tras `integrate --merge` en un nodo, la rama `<into>` se empuja al bare del nodo (`git push <bare> <into>:refs/heads/<into>`, sin `+`), y `relevo.origin` no deshace una `<into>` local más nueva que la del bare. En el Mac no cambia nada.
- `_reconcile_foreign` trae la rama del bare correcto (nombre del repo).
- N4: si `git fetch origin <rama>` falla, no empuja esa rama (aviso).
- Pruebas nuevas en `tests/test_f13_costura.py`, **sin simular `integrate`, `job_state` ni `wave_have`** (sí ssh falso por host, con alias reales tipo `nodo-a`):
  (a) dueño A lanza en una ola vía `wave.step` y muere; B toma la lease; el trabajo de A sigue vivo → B no relanza; termina `ok` → B integra y pasa a `hecha` sin relanzar;
  (b) ola de 2 + dependiente en un nodo con base remota: la dependiente arranca desde una `<into>` del bare que contiene lo integrado;
  (c) lanzador sin directorio → un `necesita_decision`, tarea no `rojo`;
  (d) `lanzador` guardado = alias de nodo, no `host:tick`;
  (e) N4 con `fetch` fallido no empuja.
- `cd delega && python3 -m unittest discover -s tests` en verde (en y es). Pruebas en **primer plano**; commitea antes de la suite completa.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/wave.py`, `queue.py`, `jobstate.py`, `relevo.py` (`origin`), `sync.py`, `delega/src/delega/i18n/*.json`, `delega/tests/test_f13_costura.py` y las pruebas existentes de N2/N13/wave/tick que haya que adaptar.
- **No tocar**: `mem.py` (si te falta algo, dilo), `launch.py`, `integrate.py`, `kindling.py`.
- Rama `claude/orq-f13`; commitea; sin push ni merge.

## Tope
- ≤ 90 min · ≤ 10 USD · 600 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤20 líneas: cómo cerraste cada hallazgo (1–6), rama y sha, pruebas y resultado (números), qué quedó sin verificar.
