# R2 revisión final: `claude/orq-mejoras` contra `main`, solo lectura

Repo `dotfiles-ia`, carpeta `delega/`. Revisa `git diff 32048d0..HEAD -- delega/` completo: fase 1 de kling, `integrate --merge --node` (+F1), `delega mem` (+F2), `tick --daemon` con lease por org (M2+F4), `delega clean` (T5), bundle mínimo y `--with-ref` (T3), relanzar y traer ramas abiertas en worktrees (T4), `gc` y `max_jobs` en kindling (F3), `delega wave` (T6+F5), relevo desde un nodo y escalado a Claude (M3+F5).

## Qué buscar (lo que de verdad importa para que el orquestador siga solo si el Mac cae)
- **Relevo**: ¿puede un segundo daemon tomar la lease mientras el primero sigue vivo (reloj, TTL, heartbeat tardío)? ¿Puede lanzarse dos veces la misma tarea (CAS `--desde` en todos los caminos de `wave` y `queue`)? ¿Queda alguna tarea atascada en `lanzada` si el dueño muere justo después de lanzar?
- **Separación de organizaciones**: ¿hay algún camino por el que una tarea o un brief de Trycore acabe en la base personal o en un nodo personal, o un secreto del Mac en la config derivada de un nodo?
- **Integración sin agente**: con `wave step` + `integrate --merge`, ¿puede fusionarse algo no verificado o perderse un commit? ¿Qué pasa si dos olas integran en la misma rama a la vez?
- **Escalado a Claude**: que nunca pueda hacer push/merge a `main`, que no se lance dos veces y que su tope se respete.
- **`clean`**: que no borre un trabajo cuya rama no esté traída (hoy usa «`fetch.log` vacío» como señal: ¿es fiable?).
- Transiciones de estado forzadas (`--forzar` en `wave` para `ok → arreglo/bloqueada`): ¿deberían ser transiciones válidas?
- Pruebas que pasan sin poder fallar.

## Terminado cuando
- Has leído el diff y escrito el informe. No cambies código ni hagas commits.

## Tope
- ≤ 40 min · ≤ 6 USD · 200 turnos. Sin subagentes.

## Informe
Hallazgos clasificados (bloqueante / importante / menor), cada uno con fichero:línea, caso concreto que falla y arreglo en una línea. Al final, una línea: «apto para fusionar a main: sí/no».
