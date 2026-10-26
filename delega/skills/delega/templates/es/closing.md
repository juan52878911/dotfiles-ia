# Cierre de trabajo: <rama>

Lo rellena el orquestador al volver el trabajador, antes de integrar o de dar el informe a {owner}.

## Procedencia
- [ ] `delega audit <rama>`: perfil, cuenta, nodo, VM, dorado, volumen(es) y sha, coste. Coinciden con el encargo.
- [ ] `delega spend` dentro del presupuesto del encargo.

## Lo que volvió
- [ ] Escaneo limpio: `delega scan <repo> <rama>` (exit 0). Si no, no se integra.
- [ ] Diff revisado a mano: ningún fixture, seed, test, snapshot o doc con datos copiados de la base
      (busca correos, teléfonos, nombres propios, ids largos, importes concretos).
- [ ] Mensajes de commit, avisos (`notices.jsonl`) e informe sin valores reales.
- [ ] Ficheros nuevos que no sean código (`*.sql`, `*.csv`, `*.json`, `*.dump`) justificados.

## Verificación
- [ ] `delega integrate <repo> <rama>`: fusión en seco y comandos `verify` del proyecto en verde.
- [ ] Lo comprobado contra la copia está en el informe con números (recuentos, tiempos), sin datos.

## Limpieza
- [ ] La VM ya no existe (`delega sandbox ls` / `kling ps` en el host) salvo `keep_vm` a propósito.
- [ ] Sandboxes de desarrollo congelados o borrados (`delega sandbox freeze|rm`).
- [ ] Si el volcado cambió: `delega golden db` para no trabajar con un dorado caducado.

## Informe a {owner}
- Qué se hizo, rama y sha, qué se verificó, qué queda, coste. Sin datos.
