# <Pieza>: <objetivo en una frase> (con BD clonada)

Encargo: `<ruta del encargo-db.md>` (criterio de aceptación congelado; no lo cambies tú).
Se lanza con `delega run <repo> <este brief> --node kindling --db <nombre>` (o `--db all`).

## Datos
- Tienes una **copia desechable** de `<nombre>` (<motor> <versión>) dentro de tu microVM; las
  variables de conexión están en tu entorno y en el contexto de delega (`DATABASE_URL`, `PG*`…).
- Son **datos reales del proyecto**. Solo existen en esta VM y se borran con ella; lo que escribas
  en la base no vuelve a ningún sitio.
- **Puedes**: leer el esquema, contar, agregar, correr migraciones y tests contra la copia, medir
  consultas (`EXPLAIN`), comprobar que un cambio funciona con datos de verdad.
- **No puedes**: copiar filas, valores, muestras, ids, correos, nombres ni capturas a ningún fichero
  del repo (fixtures, seeds, tests, snapshots, docs, comentarios), mensajes de commit, avisos ni al
  informe. Los fixtures se escriben a mano con datos inventados que imiten la forma, no el contenido.
- Nada de volcados (`pg_dump`, `SELECT … INTO OUTFILE`, `mongoexport`…) ni de ficheros de datos en el
  worktree: lo que vuelve (rama, avisos, informe) pasa por el escaneo de términos prohibidos.

## Hipótesis
- Qué crees que pasa y **cómo refutarlo rápido** contra la copia (una consulta, un test, un EXPLAIN).

## Terminado cuando
- Criterio comprobable: comando + salida esperada (recuentos, tiempos, test que pasa contra la copia).
- Criterio de parada: si tras 2 intentos no mejora, para y avisa con `blocker`.

## Zona
- Ficheros/carpetas que tocas. **No tocar**: …
- Rama `<prefijo permitido>/<nombre>`; commitea; sin push ni merge.

## Tope
- ≤ <N> min de reloj · ≤ <X> USD · sin subagentes salvo <…>.

## Informe
Termina con un informe de ≤15 líneas **sin datos**: qué cambiaste, rama y sha, qué verificaste
contra la copia (solo esquema, recuentos y tiempos: «la consulta X pasa de 2,1 s a 40 ms sobre
~1,2 M filas de `pedidos`»), qué quedó sin verificar y qué recomiendas. Si un hallazgo necesita un
valor real para entenderse, descríbelo sin el valor («hay emails con mayúsculas en `clientes.email`»).
