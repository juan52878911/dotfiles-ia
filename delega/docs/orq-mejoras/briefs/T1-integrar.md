# T1 integrar: `delega integrate --merge --node <n>` fusiona sin agente cuando la suite sale verde

Repo `dotfiles-ia`, carpeta `delega/`. Línea base: `main` en 32048d0.

## Contexto
Hoy `delega integrate` (`delega/src/delega/integrate.py`, `main(argv)`) hace un ensayo de fusión de la rama de un trabajo y corre los `verify` del proyecto. En platform-fundations la integración la hacía un agente por rama, en serie: 44 integraciones, mediana 18,2 min, el 43 % del tiempo de cada ola. Queremos que lo normal (verde) no necesite agente.

## Hipótesis
- Basta con añadir `--merge` (fusiona de verdad en la rama destino `--into`, por defecto la de hoy) y `--node <n>` (corre los `verify` en ese nodo por ssh, en un worktree temporal del repo bare `~/<workdir>/<repo>.git`, y lo borra al acabar). Refútalo leyendo `integrate.py` y `remote.py` antes de escribir.

## Terminado cuando
- `delega integrate <repo> <rama> --merge [--into <destino>] [--node <n>]`:
  - verde → fusiona en el destino (commit de merge `--no-ff`), escribe el resultado en `audit` y sale **0**;
  - `verify` rojo → **no** toca la rama destino, deja el registro de la suite en el estado del trabajo y sale **7**;
  - conflicto → no toca la rama destino, lista los ficheros en conflicto y sale **8**.
- Pruebas nuevas en `delega/tests/test_ops_integrate_sync.py` con el `ssh` falso de `tests/fakes/`, que fallan sin el cambio: los tres casos de arriba, más «sin `--node` corre local como hoy».
- `cd delega && python3 -m unittest discover -s tests` en verde.
- Criterio de parada: 2 intentos sin verde → `blocker`.

## Zona
- `delega/src/delega/integrate.py`, `delega/tests/test_ops_integrate_sync.py`, y las claves nuevas de `delega/src/delega/i18n/{en,es}.json` con prefijo `integrate.`.
- **No tocar**: `launch.py`, `queue.py`, `kindling.py`, `mem.py`, `skills/**` (otros trabajos).
- Rama `claude/orq-integrar`; commitea; sin push ni merge a `main`.

## Tope
- ≤ 60 min de reloj · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, las pruebas nuevas y su resultado (números), qué quedó sin verificar y qué recomiendas.
