# F6 wave: que una ola real funcione de punta a punta

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `claude/orq-mejoras` (todo integrado, 351 pruebas OK en en y es). Arreglas hallazgos de la revisión final R2 (Opus). Lee el código citado antes de cambiarlo.

## Hallazgos
1. **Bloqueante** `wave.py:189` `Env.launch` pasa el *texto* del brief como ruta; `delega run` hace `open(a.brief)` (`launch.py:621`) y lo rechaza: todo pasa a `rojo`. Las pruebas usan solo `FakeEnv`. Arreglo: escribir el brief en `state_dir/wave/<org>/<flujo>-<id>.md` y pasar esa ruta; prueba con el `Env` real y un `delega run` falso en PATH.
5. **Importante** `Env.launch` no pone `DELEGA_FROM_QUEUE=1`: con nodo lleno `delega run` encola en `queue.jsonl` y la tarea queda `rojo`. Arreglo: poner esa variable y tratar rc 6 como «sigue `pendiente`» (devolver la tarea con CAS `lanzada → pendiente`).
6. (parte de wave) `integrate` va a devolver **rc 11** cuando el destino se mueve (lo cambia otro trabajo): `wave` debe tratar 11 como «reintentar en el siguiente step», no como conflicto.
7. **Importante** `wave.py:343`: en rc 7 lanza el arreglo *antes* del CAS `ok → arreglo`; dos `step` a la vez lanzan dos arreglos. Arreglo: CAS primero, lanzar solo si se gana.
8. **Importante** `wave.py:330-345`: integra `t["rama"]` local sin comprobar que el fetch del job salió bien ni el sha. Arreglo: `job_state` exige que el trabajo trajo su rama (sin error en `fetch.log`; si se trajo a `refs/delega/<id>`, usar esa ref) y guarda el sha traído en la tarea; se integra ese sha.
9. (parte de wave) `wave.py:426`: `--repo-dir` por defecto es el cwd y no se compara con `flujos.repo`. Arreglo: exigir que el `origin` de `--repo-dir` coincida con el repo del flujo (normalizando `git@`/`https`/`owner/name`) y que el perfil de la tarea sea de la org de la base; si no, rc 2.
14. **Importante** una tarea `rojo` (job fallido) se queda así para siempre. Arreglo: `rojo` con `intentos < 2` → un reintento como arreglo (`rojo → arreglo` con CAS); con `intentos ≥ 2` → `bloqueada` + `necesita_decision`.
19. **Menor** transiciones forzadas `ok → arreglo`/`ok → bloqueada`: no las fuerces; si `mem` no las admite, pídelo en el informe (otro trabajo lo añadirá) y mientras usa `--forzar` solo con `--desde ok`.

## Reglas de ejecución (importante)
- Corre las pruebas **siempre en primer plano** (Bash normal con timeout de hasta 600000 ms). **Nunca** en segundo plano, con `run_in_background`, Monitor ni esperando notificaciones: si terminas el turno esperando, delega cierra el trabajo y se pierde lo no commiteado (le pasó al primer intento de este mismo trabajo).
- **Commitea en cuanto tus pruebas nuevas pasen**, antes de lanzar la suite completa (~230 s por idioma); si la suite completa falla, arregla y haz otro commit.

## Terminado cuando
- Una prueba por hallazgo que falla sin el arreglo; para los que R2 marcó como «prueba que no puede fallar», la prueba usa el camino real (no el doble).
- `cd delega && DELEGA_LOCALE=en python3 -m unittest discover -s tests` y lo mismo con `DELEGA_LOCALE=es`, los dos en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/wave.py`, `delega/tests/test_wave.py`, claves `wave.` en i18n. **No tocar**: `queue.py`, `mem.py`, `integrate.py`, `relevo.py`, `launch.py`.
- Rama `claude/orq-f6`; commitea; sin push ni merge.

## Tope
- ≤ 60 min · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
≤15 líneas: qué cambiaste por hallazgo (número), sha, pruebas (números), qué quedó sin verificar.
