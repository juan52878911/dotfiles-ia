# F2 arreglos de R1 en `delega mem`

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras`. Solo `mem.py` y su prueba.

## Hallazgos de la revisión R1 (Opus) que arreglas
1. **Bloqueante** `mem.py:151`, `cmd_heartbeat`, `cmd_release`: el dueño es `host:pid`; por ssh cada orden es un proceso nuevo, así que el `heartbeat` siguiente a un `claim` choca con su propia lease (rc 9) y `release` no la suelta. Arreglo: `--owner <id>` o `$DELEGA_MEM_OWNER` **obligatorio** en claim/heartbeat/release (rc 2 si falta). Prueba con tres procesos distintos (claim, heartbeat, release) y el mismo owner.
2. **Importante** `mem.py:413` `set_task`: lee fuera de la transacción y no valida transiciones. Arreglo: leer dentro de `write()`, opción `--desde <estado>` como compare-and-set (si no coincide, rc 9), tabla de transiciones válidas (una `hecha` no vuelve atrás salvo `--forzar`), e `intentos` se incrementa en la misma transacción al pasar a `lanzada`.
3. **Importante** `mem.py:75`, `add_flow`/`add_task`: `--org` es opcional y la base por defecto es la misma para todas las orgs. Arreglo: exigir `--org` o `$DELEGA_MEM_ORG` en toda orden que escribe y comprobar que coincide con la de la base; la base por defecto pasa a `~/.local/state/delega/memoria-<org>.db`.
4. **Menor** `mem.py:834`: «database is locked» devuelve rc 2 como un error de validación. Arreglo: rc **10** para bloqueo (`sqlite3.OperationalError` locked/busy), para que quien llama reintente.
5. **Menor** `mem.py:689`: `--ttl inf` o negativo se acepta. Arreglo: TTL finito, > 0 y ≤ 3600.
6. **Menor** `mem.py:171`: `SHORT_RE` acepta `repos/app`. Arreglo: rechazar si `os.path.exists(repo)` o si empieza por `.`/`/`/`~`.
Actualiza el docstring de la interfaz (códigos 0/2/9/10, `--owner`, `--desde`, `--org`).

## Terminado cuando
- Una prueba por hallazgo en `delega/tests/test_mem.py` que falla sin el arreglo; las 19 de antes siguen en verde (ajústalas solo si cambió la interfaz a propósito).
- `cd delega && python3 -m unittest discover -s tests` en verde.

## Zona
- `delega/src/delega/mem.py`, `delega/tests/test_mem.py`, claves `mem.` en i18n. **No tocar** nada más.
- Rama `claude/orq-f2`; commitea; sin push ni merge.

## Tope
- ≤ 45 min.

## Informe
≤15 líneas: qué cambiaste por hallazgo, sha, pruebas (números), interfaz final.
