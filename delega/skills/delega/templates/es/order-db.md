# Encargo con datos reales: <nombre>

> Se acuerda con {owner} **antes** del primer trabajador. Además de lo del encargo normal
> (`order.md`), fija qué datos reales entran y cómo se controlan.

## Objetivo y entregable
- Qué existe al final que hoy no existe (PR, informe, métrica). Uno solo.

## Datos que entran (OK de {owner}: <fecha>)
- Proyecto `<org/repo>` · base `<nombre>` (<motor> <versión>) · volumen `db-<repo>-<nombre>` en `<nodo>`.
- Volcado: `<de dónde sale>` (comando en el host o fichero), fecha `<…>`, sha256 `<12 primeros>`
  (`delega db ls`). Se refresca con `delega db volume refresh` y **solo con OK de {owner}**.
- Anonimizado: <sí / no / parcial: qué columnas>. Tablas fuera: <…> (si hace falta, un volcado con `--exclude-table`).
- Modo: dorado de datos (`delega golden db`, arranque ~1 s) o volumen (restauración por trabajo).

## Criterio de aceptación (congelado)
- Entrada → salida esperada, medido contra la copia. Nada del criterio cita valores reales.
- Hash de las expectativas: `shasum -a 256 <fichero>` = `…`

## Controles
- Egress de la VM: lista blanca (`egress_allow` del nodo). Nada sale salvo rama, avisos e informe.
- Escaneo de términos prohibidos en la rama y en lo que vuelve; términos nuevos de este dominio
  añadidos a `forbidden.txt`: <…>.
- Revisión humana del diff antes de integrar: ¿hay fixtures o tests nuevos? ¿datos inventados?
- Auditoría: `delega audit <rama>` muestra volumen, sha, dorado, VM y coste de cada trabajo.

## Presupuesto
- Reloj: ≤ … h · gasto de la cuenta: ≤ … $ (`delega spend`).

## Fuera de alcance
- Exportar datos, informes con datos, cambios en la base real.

## Cambios
| Fecha | Qué cambió | Por qué | OK de {owner} |
|---|---|---|---|
