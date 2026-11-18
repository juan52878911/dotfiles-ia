# T0 fase 1: fusionar `claude/kling-fase1-b556de` sobre `main` y dejar la suite estable

Repo `dotfiles-ia`, carpeta `delega/` (el paquete Python de delega). Línea base: `main` en 32048d0.

## Contexto
La rama `claude/kling-fase1-b556de` (4 commits: reintentos de ssh y rc 96 sin destruir, cupo real, TTL en las VMs, recover y gc; lease medido con el reloj del nodo; con el nodo lleno el brief se encola una vez con rc 6 en vez de dejar un trabajo `launch_failed`) lleva días sin integrar. Un análisis previo vio que fusiona limpia. En `main`, la suite (`cd delega && python3 -m unittest discover -s tests`, 245 pruebas, ~150 s) dio 1 fallo en una pasada y 0 en la siguiente: hay una prueba inestable sin identificar.

## Hipótesis
- La fusión no tiene conflictos; si los tiene, son de `i18n/*.json` o `ARCHITECTURE.md`. Refútalo con `git merge --no-commit`.
- La prueba inestable depende del reloj, del orden o de un puerto/fichero temporal compartido. Refútalo corriendo la suite 5 veces seguidas (`for i in 1 2 3 4 5; do …; done`) y anotando qué prueba falla.

## Terminado cuando
- Rama `claude/orq-fase1` = `main` + fusión de `claude/kling-fase1-b556de` (+ los arreglos mínimos que hagan falta), commiteada.
- La suite pasa **5 veces seguidas** en verde; la prueba inestable está identificada (nombre) y arreglada sin desactivarla ni subir tiempos de espera a ciegas, con una línea en el commit que explique la causa.
- La prueba existente de la rama «nodo lleno → rc 6, una entrada en la cola y ninguna carpeta de trabajo» pasa.
- Criterio de parada: si tras 2 intentos la inestabilidad no se reproduce, deja la fusión verde, apunta en el informe el nombre de la prueba sospechosa y avisa con `blocker`.

## Zona
- `delega/src/delega/{launch,queue,kindling,jobs,remote,config,__main__}.py`, `delega/src/delega/i18n/*`, `delega/tests/**`, `delega/skills/**`, `delega/ARCHITECTURE.md`.
- **No tocar**: `delega/src/delega/integrate.py` (lo cambia otro trabajo en paralelo), `delega/src/delega/mem.py` (nuevo, de otro trabajo), nada fuera de `delega/`.
- Rama `claude/orq-fase1`; commitea; sin push ni merge a `main`.

## Entorno
- Solo Python de la biblioteca estándar y `ruff` si está. Sin red salvo la del propio git.

## Tope
- ≤ 60 min de reloj · ≤ 6 USD · 300 turnos. Sin subagentes.

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, la prueba inestable y su causa, resultado de las 5 pasadas (números), qué quedó sin verificar y qué recomiendas.
