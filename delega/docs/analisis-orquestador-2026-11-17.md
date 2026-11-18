# Análisis del orquestador (delega + skills + kindling) y plan de un día

17-nov-2026. Solo lectura: no se cambió código, configuración ni nodos. Fuentes: `~/.local/state/orquestar-trycore/` (1.671 carpetas de trabajo, `audit.jsonl` con 3.970 líneas, `limits.tsv` con 2.034 muestras), `delega/ARCHITECTURE.md`, los skills `orquestar-trycore`, `orquestar-personal` y `delega`, `~/.config/delega/config.toml`, el repo `kindling` (v1.0.0-rc.2) y `platform-fundations` (`docs/07-ejecucion/` y `.orquestador/finde.sh`). Los hechos medidos hoy por Juan (nodos, versiones de kling, capacidad) se usan sin volver a medirlos.

Marcas: **COMPROBADO** = sale de un dato o de código leído hoy (se cita la fuente); **OPINION** = estimación o juicio; **DESCARTADO** = hipótesis que los datos no sostienen.

Nota de ubicación: `~/Documents/GitHub/delega` es la carpeta `delega/` del repo `dotfiles-ia` (remoto `juan52878911/dotfiles-ia`, `ws contexto` = `personal-plus/dotfiles-ia`). Esto condiciona el plan: ver §6, «Ruta».

## 1. Resumen ejecutivo

1. **El orquestador real no es delega, es un script de proyecto.** Las 32 olas de platform-fundations (13-nov 21:49 a 15-nov 07:51, 34,0 h) las condujo `.orquestador/finde.sh` (242 líneas de bash con `sleep` de 30/45/60/600/900/1800 s). delega no tiene olas, dependencias ni integración encadenada: `queue.jsonl` tiene 7 entradas y `integrate` 34 eventos frente a 989 trabajos ok.
2. **El paralelismo efectivo fue 1,17**, no 2 ni 6: 39,9 horas de trabajador en 34,0 h de reloj. El plan de 3 días supone *E* = 2,4.
3. **La integración es serie y la hace un agente**: 44 trabajos de integración, mediana 18,2 min; en las 28 olas con las dos fases, 702 min de integración frente a 915 min de trabajadores (43 % del tiempo de ola).
4. **El 22,5 % de los lanzamientos de la era delega no arranca** (316 `launch_failed` de 1.404): 116 por memoria del host de kindling, 64 por `max_local_jobs`, 64 por ramas ya usadas por un worktree viejo, 32 por carga o `max_jobs` del PC. Casi todos son bucles de relanzamiento. La rama `claude/kling-fase1-b556de` (4 commits, fusiona limpia) arregla el caso «nodo lleno» y lleva tres semanas sin integrar.
5. **La ventana de 5 h no fue el límite con Sonnet**: pico de 32 % el 13-nov y 31 % el 14-nov. Solo se saturó los dos días de Fable (27-oct 83 %, 5-nov 106 %).
6. **Relanzar sobre la misma rama costó 420,8 USD** (27,7 % del gasto total de 1.520,4 USD) y Fable 357,1 USD con 50 % de éxito.
7. **kindling no sirve hoy para el camino crítico de Trycore**: host en v0.18, nodo `kindling` con 10 ok de 18, y el fallo de 64 MB es un defecto de delega (sube los 303 MB de historia sin necesitarlo), no de la versión de kling.

## 2. Diseño propuesto frente a realidad

| Componente | El diseño dice | Pasa en los trabajos (cifra y fuente) | Hueco |
|---|---|---|---|
| **Lanzamiento** | `delega run` valida identidad, presupuesto, lint, zonas y nodo, y lanza (`launch.py`). rc 2 a 6 según el rechazo. | 316 `launch_failed` y 629 `refused` en `audit.jsonl`. Arranque 4 s en trycorepc y 11 s en kindling (id del trabajo → `summary.start`). Entre lanzamientos de una ráfaga pasan 48 s de mediana (n = 116), por el `sleep 45` de `finde.sh:174`. | Cada intento fallido deja una carpeta `launch_failed` y el llamador reintenta en bucle (ola 23: 45 lanzamientos, 37 fallidos, 167 min). Falta «encolar una vez» (está en la rama fase 1). |
| **Nodos y colas** | `--node auto` por `auto_order`; `queue`/`tick` lanzan cuando hay hueco (`queue.py`). | `tick` no lo llama nadie (no hay launchd ni bucle); 7 entradas en `queue.jsonl`. trycorepc llevó 739 trabajos (82 % ok); dev-trycore 116 (71 %), rechazada 27 veces por disco y 28 por inalcanzable. 64 rechazos por rama ya en uso en un worktree viejo; 465 y 203 carpetas viejas en los nodos. | La cola no tiene dependencias ni olas; no hay limpieza de worktrees en nodos host (`gc` de la fase 1 solo cubre VMs). |
| **kindling como runner** | Primero en `auto_order` por defecto; una microVM por trabajo desde un dorado; solo viaja lo que HEAD tiene sobre la base del dorado. | kindling-personal 139 trabajos, 88 % ok, mediana 12,3 min, API/reloj 0,28. kindling-personal-nav 146, 14 % ok (115 «doesn't fit» el 26–27 oct). `kindling` (Trycore) 18, 10 ok. 13 `broker_lost`, 10 de ellos en kindling. | Sin TTL ni etiquetas en las VMs, sin `recover`/`gc`, sin reintentos de ssh (todo en fase 1). `push_head` sube toda la historia si la base del dorado no es ancestro de HEAD (§4). |
| **Canal y broker** | `inbox`/`notices`/`progress`/`state`/`lease`; `watch --bus` como único monitor. | 48 cierres sintéticos: 32 `not_started` (27 en trycorepc), 13 `broker_lost`, 3 `no_summary`. Cierre 14 s en trycorepc (fin del worker → `END`). | `not_started` no dice por qué (falta el motivo en `summary`). El 30 % de turnos vacíos del orquestador (medido en oct) no se ha vuelto a medir. |
| **Revisión e integración** | `delega integrate`: fusión en seco y `verify` del proyecto en un worktree desechable del Mac; revisión Opus una por épica. | 34 eventos `integrate` (30 del perfil personal, 4 de Trycore; 23 `clean` sin verificar). En platform-fundations integró un agente Sonnet por ola: 44 trabajos, mediana 18,2 min, 13,2 h, 26,6 USD. 2 trabajos Opus en todo el proyecto. | `integrate` no fusiona de verdad, no corre en un nodo (el Mac no levanta la suite) y no encadena con la ola. La zona `**` del integrador bloquea todo lo demás (53 rechazos «`**` ↔ `**`»). |
| **Métricas y observabilidad** | `metrics report`, `spend --check`, chrono-ia, informe `recursos-por-nodo`. | El último informe es del 30-oct (846 trabajos). No hay métrica de paralelismo, de hueco entre olas ni de fase de integración: se calcularon hoy a mano. `summary.json` existe en 1.387 de 1.671 carpetas (las 264 que faltan son del 20–23 oct, anteriores a delega). | Falta el informe por ola (paralelismo, espera, integración) que diga dónde se va el reloj. |
| **Presupuesto y cupo** | `budget.day_usd` = 150; `quota` deriva el ritmo de los `rate_limit_event`. | Gasto del 13, 14 y 15 de nov: 66,5 / 78,5 / 53,5 USD. 8 rechazos por presupuesto en toda la historia. 5 h: máximo 32 % en los días de platform-fundations; semanal 6 % → 30 % en esos tres días. | El plan de 3 días calcula 1.368 USD y 3 cuentas; el coste real por trabajo Sonnet (mediana 0,57 USD en platform-fundations) es unas 5 veces menor que su supuesto. El cupo no reparte huecos: `max_jobs` es fijo. |
| **Bundle y sync** | `sync` empuja reglas a los nodos; a kindling viaja `base..HEAD`. | 1 fallo por 64 MB (`20261113-004159-36847`, Oriuno). 54 `golden_stale` y 3 rehechos de dorado fallidos. Las historias de front de la ola 05 «llegaron sin ejecutar ninguna prueba (el nodo no tenía red)» (`ola-05.resultado`). | No hay preparación del proyecto en el nodo (dependencias instaladas) ni comprobación de que el worker ejecutó la suite antes de dar `ok`. |

## 3. Trabajos reales

### 3.1 Cifras agregadas

1.668 carpetas con `meta` legible; 264 son del 20–23 oct sin `summary.json` (anteriores a delega) y quedan fuera de las tasas. Era delega: **1.404 trabajos, 989 ok (70,4 %), 1.520,4 USD**. De los 1.082 que llegaron a arrancar un worker, 989 ok (91,4 %).

| Desenlace | Trabajos | Coste USD |
|---|---|---|
| ok | 989 | 1.276,5 |
| `launch_failed` | 316 | 0 |
| sintético (`not_started` 32, `broker_lost` 13, `no_summary` 3) | 48 | 10,5 |
| rc 1 | 20 | 30,3 |
| cortado por tope | 17 | 203,1 |
| `dead` | 8 | 0 |
| abierto | 6 | 0 |

| Nodo | Trabajos | ok | Mediana ok (min) | Herr./min | API/reloj | Coste USD | USD por ok |
|---|---|---|---|---|---|---|---|
| trycorepc | 739 | 82 % | 7,5 (p90 23,6) | 3,51 | 0,47 | 614,6 | 1,02 |
| local (Mac) | 234 | 62 % | 11,9 (p90 34,6) | 4,23 | 0,69 | 618,9 | 4,24 |
| kindling-personal-nav | 146 | 14 % | 17,9 (p90 65,7) | 1,81 | 0,17 | 25,9 | 1,23 |
| kindling-personal | 139 | 88 % | 12,3 (p90 39,7) | 2,22 | 0,28 | 105,1 | 0,85 |
| dev-trycore | 116 | 71 % | 3,8 (p90 20,9) | 5,77 | 0,83 | 106,8 | 1,30 |
| kindling (Trycore) | 18 | 56 % | 3,2 | 9,55 | 0,87 | 47,8 | 4,78 |
| kindling-pve-personal | 7 | 29 % | 30,7 | — | — | 1,3 | 0,66 |

«API/reloj» es `duration_api_ms / duration_ms` de `result.json`: en trycorepc el modelo ocupa el 47 % del reloj del trabajo; el resto son herramientas (suites, builds) y esperas. En kindling-personal el modelo es el 28 %: las herramientas tardan más ahí. Solo noviembre: trycorepc 522 trabajos, 76 % ok, mediana 9,6 min; dev-trycore 39, 92 %; kindling-personal 43, 88 %, 10,1 min.

| Repo | Trabajos | ok | Mediana (min) | Coste USD | USD por ok |
|---|---|---|---|---|---|
| chispa | 406 | 88 % | 5,2 | 280,4 | 0,79 |
| platform-fundations | 380 | 74 % | 10,6 | 262,2 | 0,93 |
| Oriuno | 369 | 69 % | 8,9 | 801,6 | 3,17 |
| CRM_Aura | 139 | 9 % | 27,3 | 24,2 | 1,86 |
| community-day-kit | 42 | 86 % | 10,9 | 19,1 | 0,53 |

| Modelo | Trabajos | ok | Coste USD | USD por ok |
|---|---|---|---|---|
| Sonnet 5.5 | 980 | 96 % | 1.064,3 | 1,14 |
| Fable 5.1 | 40 | 50 % | 357,1 | 17,86 |
| Opus 5.5 | 30 | 100 % | 86,4 | 2,88 |

Relanzamientos (misma rama y repo, trabajos que arrancaron worker): 944 ramas; 870 con un solo trabajo, 74 con más de uno; 138 trabajos extra que costaron **420,8 USD**. `--resume` se usó 43 veces.

### 3.2 platform-fundations, ola a ola

Olas 1 a 32 (ramas `feat/wNN-…` e `integra/ola-NN…`), en trycorepc con `max_jobs` = 2:

- Reloj: 34,0 h. Trabajo: 165 historias ok (mediana 7,6 min, p90 21,0; 26,7 h; 89,4 USD) y 44 integraciones ok (mediana 18,2 min; 13,2 h; 26,6 USD). Paralelismo medio 1,17.
- Día 14-nov completo: 27,7 h de trabajador en 23,6 h; 244 min sin ningún worker, 847 min con uno, 151 con dos, 172 con tres.
- En 28 olas con las dos fases: fase de trabajadores 24 min de mediana (suma 915 min), fase de integración 21 min (suma 702 min).
- Ritmo: 165 trabajos de historia en 34 h = 4,9 por hora. El plan de 3 días necesita 606 trabajos en 56 h = 10,8 por hora: **2,2 veces el ritmo medido**.
- Cupo de la cuenta en esos días: 5 h máx. 32 % / 31 % / 19 %; semanal de 6 % a 30 %.

### 3.3 Los cinco modos de fallo más caros

1. **Relanzar sobre la misma rama sin cerrar el problema: 420,8 USD.** `Oriuno fix/pdd-idioma-reglas-tramos`: 9 trabajos, 96,0 USD (`20261104-203233-78026`, `20261104-211425-8108`, `20261104-211648-19908`). `Oriuno fix/pdd-roles-y-relleno`: 6 trabajos, 29,3 USD (`20261103-175622-13824`). El skill pone el corte en dos iteraciones por hipótesis; delega no lo cuenta ni lo avisa.
2. **Fable cortado por tope: 13 trabajos, 184,6 USD sin resultado utilizable** (de 17 cortes y 203,1 USD). `20261027-125300-62815` (36,0 USD), `20261027-122432-22716` (31,6), `20261026-090343-19242` (21,4). Son además los únicos días con la ventana de 5 h por encima del 80 %.
3. **Bucles de `launch_failed`: 316 carpetas, 0 USD de worker, horas de reloj.** 116 «doesn't fit» (`20261026-213600-78837` … `20261027-151126-46533`, CRM_Aura en kindling-personal-nav); 64 `max_local_jobs` (`20261030-110134-92588`, `20261115-003700-36088`); 32 por carga o `max_jobs` de trycorepc (`20261104-095622-27178`, `20261028-205805-48477`). En platform-fundations: 68, de ellos 51 el 14-nov. El coste no medido es el de los turnos del orquestador que reintenta.
4. **Rama ya usada por un worktree viejo: 64 lanzamientos perdidos.** «already used by worktree» 40 (`20261028-225843-46977`, `20261117-142256-37280`) y «already checked out» 24 (`20261029-002600-40152`). El worktree del trabajo anterior sigue en el nodo; nadie lo retira. Mismo origen que el disco lleno de dev-trycore (30 rechazos por disco) y el `No space left on device` de `20261105-103945-4427`.
5. **Trabajos que arrancan y se pierden: 48 sintéticos + 20 rc 1 + 8 `dead`.** `not_started` 32 (27 en trycorepc: `20261102-120559-35372`, `20261117-001529-90432`); `broker_lost` 13 (10 en kindling: `20261026-213857-89969`, `20261106-172006-2961`). Una ráfaga de 11 rc 1 cada 30 s el 13-nov 04:44–04:51 (`20261113-044427-14886` … `20261113-045107-16765`, 0,1 USD cada uno): un bucle relanzando algo que fallaba igual. La causa de esos rc 1 no se investigó hoy.

## 4. Kindling

### Versiones por host

| Host | kling | Estado |
|---|---|---|
| Mac | 1.0.0-rc.2 (2026-11-15) | al día |
| kling-ascci | v0.18.0-26-g9a34d392 (binario del 24-oct) | 1.018 commits por detrás de rc.2; en uso |
| kling-pve (CT 109) | sin medir | apagado |
| lab (<ip-lan>) | sin medir | caído |

### Qué cambia de v0.18 a v1.0.0-rc.2 en lo que toca a delega

COMPROBADO en `kindling/CHANGELOG.md` y `git log v0.18.0..v1.0.0-rc.2`:

- `d8b6add5` «cap exec ?wait=1 output at 8 MiB per stream»: es un límite nuevo. La fase 1 de delega ya lo evita (la rama vuelve por `kling cp`, nunca por stdout).
- `94ce5e24` «Squeeze and freeze idle machines under host memory pressure» y admisión por memoria real: ataca los 116 «doesn't fit». El texto del error se conserva (`internal/machine/memory.go:340`), así que la detección por texto de la fase 1 sigue valiendo.
- `a384d90a` «kling volume clone with reflink and a filesystem clone probe»: afecta al clon lento sin reflink de CT 109 (4–8 min). No se comprobó que resuelva el caso NFS.
- `9f167feb`, `ae9d7cf2`, `ff619d9d`, `2bfa5030`: el planificador retira o congela la máquina que no arranca, renueva los TTL y readopta el VMM tras un reinicio. Tocan el origen de los `broker_lost`.
- `ad25c74d` «idle deadline on blob uploads» (corte a los 2 min sin datos).
- 1.0.0: códigos de error estables (`insufficient_memory`, `stale_golden`, `disk_full`), `-json` en los comandos que cambian algo (`091e6ed3`), códigos de salida documentados (4 = dorado caducado).
- Riesgo de la actualización: `be84f448` exige firmas en las plantillas por defecto y migra v1 a v2, y la 1.0 promete leer formatos de disco «desde N-1». El host está tres menores por detrás (0.18 → 0.19 → 0.20 → 1.0). **OPINION**: hay que contar con rehacer los 10 dorados de kling-ascci; `docs/actualizar.md` (597 líneas) no se leyó entero.

Lo que arregla la rama `claude/kling-fase1-b556de` de **delega** (4 commits sobre `88ad91f`, 22 ficheros, +878 −85; `git merge-tree` contra `main` no da conflictos): `4b5f29f` reintentos de ssh, rc 96 sin destruir la VM, TTL y etiquetas en las VMs, `recover` y `gc`; `380e33b` la edad del lease con el reloj del nodo; `999f9ae` nodo lleno → rc 6 y el brief se encola una sola vez. El último vale también para trycorepc, no solo para kindling.

### El límite de 64 MB

- **COMPROBADO**: el límite es una constante, `FileMaxUpload = 64 << 20` en `kindling/pkg/api/exec.go:42`, también en v1.0.0-rc.2. **DESCARTADO**: que actualizar kling lo resuelva.
- **COMPROBADO** la causa en delega: `kindling.py:147-155` empaqueta `base..HEAD` solo si la base del dorado es ancestro de HEAD; si no, empaqueta **todo HEAD**. En `20261113-004159-36847` el trabajo salió del checkout principal de Oriuno, con HEAD `0dcecf84` (14-oct), que va **710 commits por detrás** de la base del dorado `6ebf1577` (5-nov). HEAD es ancestro de la base: la VM ya tenía todos los objetos y no hacía falta subir nada; delega subió los 303,54 MiB.
- Arreglos, de menor a mayor esfuerzo:
  1. Si HEAD es ancestro de la base del dorado: no subir; `git branch -f` en la VM. Si divergen: empaquetar `HEAD --not <merge-base>`. Unas 10 líneas más prueba.
  2. Si aun así pasa de 64 MB: partir el bundle con `split` y unirlo en la VM (el mismo patrón que ya usan los volcados de bases, `dump.<ext>.part-*`).
  3. Avisar en el lint si HEAD va por detrás de `origin` (el trabajo salía de un checkout de hace un mes).
  4. Dorado al día: con la base reciente, `base..HEAD` son pocos KB. Ya existe `golden.auto_rebuild`; está en `false`.
- **DESCARTADO** por ahora: clon superficial (rompe `merge-base` y el bundle de vuelta) y repo en un volumen compartido (las plantillas restauradas no admiten discos nuevos, según `ARCHITECTURE.md`).

### ¿Seguir usándolo en el camino crítico?

- Trycore, camino crítico: **no** (OPINION apoyada en datos). 10 ok de 18 en el nodo `kindling`, dorado `claude-oriuno` con lockfiles viejos, host en v0.18, sin `--resume`, y las herramientas tardan más (API/reloj 0,28 frente a 0,47). El plan de 3 días ya lo trata como apoyo lento.
- Personal: sí, como hoy. kindling-personal da 88 % de ok tanto en el total como en noviembre.
- Actualizar kling-ascci merece un hueco propio, con los dorados rehechos y un trabajo de humo por dorado; no el mismo día en que se dependa de él.

## 5. Mejoras priorizadas

Ahorro estimado sobre una ejecución como la medida (32 olas, 34,0 h). Las cifras de ahorro son OPINION construidas sobre medidas COMPROBADAS; el esfuerzo es en trabajos de delega.

| # | Mejora | Base medida (COMPROBADO) | Ahorro estimado (OPINION) | Esfuerzo | Riesgo |
|---|---|---|---|---|---|
| M1 | **Integrar la fase 1** (rc 6 + cola única, reintentos ssh, `recover`, `gc`, TTL) | 316 `launch_failed`; ola 23 con 37 fallidos en 167 min; 13 `broker_lost` | 2–3 h y casi todos los turnos de reintento | 1 trabajo + OK de merge | Medio: toca `launch.py`, `jobs.py`, `kindling.py`; 245 pruebas; hoy una pasada dio 1 fallo y la siguiente ninguno |
| M2 | **`delega integrate` de verdad**: fusiona, corre `verify` en un nodo y solo llama a un agente si sale rojo | 44 integraciones por agente, mediana 18,2 min, 702 min de fase; la suite entera tarda 3 min 31 s de backend más el front (`cierre-plan-completo.md`) | 4–8 h | 1 trabajo | Medio: cuántas olas salen verdes sin arreglo no está medido |
| M3 | **`delega wave`**: una ola desde un TSV (fila = trabajo con zona), sin `sleep`, espera por eventos, encadena con M2 | `finde.sh` con `sleep 45` entre lanzamientos (48 s medidos) y sondeos de 30–60 s; 244 min sin ningún worker el 14-nov | 2–3 h, y quita el script por proyecto | 1 trabajo grande | Medio-alto: es código nuevo; se valida con un piloto |
| M4 | **Más huecos donde el cupo lo permite**: trycorepc a 3 (hecho hoy) y dev-trycore con disco libre | Paralelismo 1,17 con pico de 5 h del 32 %; fase de trabajadores 24 min con p90 de trabajo de 21 min | ~4 h (la fase baja hacia la duración del trabajo más largo) | Configuración y limpieza | Bajo. Con 3 workers Sonnet sostenidos la extrapolación lineal da ~80 % de la ventana |
| M5 | **Rama en uso y limpieza de nodos**: retirar el worktree de un trabajo cerrado al relanzar su rama; `delega clean` en nodos host | 64 lanzamientos perdidos; 30 rechazos por disco; 465 y 203 carpetas | 1 h y dev-trycore deja de caerse por disco | 2 trabajos pequeños | Bajo: borrar solo trabajos con `END` y rama ya traída |
| M6 | **Puerta de verificación al cerrar el trabajo**: `verify` del proyecto en el nodo antes de dar `ok`, resultado en `summary.json` | Ola 05: front sin ejecutar pruebas; el `ok` de hoy es solo rc 0 | Hace posible M2 sin agente; ahorro incluido allí | 1 trabajo | Medio: alarga cada trabajo lo que dure la suite |
| M7 | **Arreglo del bundle de kindling** (§4) | 1 fallo, causa identificada | Desbloquea Oriuno en kindling; 0 h en platform-fundations | 1 trabajo pequeño | Bajo |
| M8 | **Informe por ola** en `delega metrics` (paralelismo, espera, fase de integración) | Hoy se calculó a mano | No ahorra reloj; permite ver si M2–M4 funcionan | 1 trabajo pequeño | Bajo |
| M9 | **Freno a los relanzamientos**: avisar a la tercera vuelta sobre la misma rama y exigir motivo para Fable | 420,8 USD en segundas y siguientes vueltas; Fable 357,1 USD con 50 % ok | Dinero, no reloj | Dentro de M5 o del lint | Bajo |

Suma de M1–M5: de 34 h hacia **18–22 h** para las mismas 32 olas (OPINION). No se puede comprobar sin ejecutar el piloto.

Afirmaciones sobre capacidad:

- **DESCARTADO** con Sonnet: «una cuenta sostiene ~1 trabajador». Con 1,18 workers de media el 14-nov la ventana de 5 h llegó al 31 % (`limits.tsv`). Sí se sostiene con Fable (83 % y 106 % con 3,1 y 7,1 h de trabajador). Límite de la medida: `limits.tsv` no separa otro uso de la cuenta.
- **OPINION**: el límite que se acerca primero con 3 workers continuos es el semanal: 24 puntos en unas 55 h de trabajador (13–15 nov) son ~0,44 % por hora; 3 workers × 72 h = 216 h ≈ 95 %.
- **DESCARTADO**: que el presupuesto de 450 USD al día sea lo que alarga el plan de 3 días. El día más cargado gastó 78,5 USD.
- **COMPROBADO**: frente de 6 y `contract_changed` siguen sin soporte (R8 del plan); ninguna de las mejoras de arriba lo añade. M3 da el frente; el aviso de contrato queda fuera de este día.

## 6. Plan de un día, ejecutado con delega

### Ruta (decisión previa de Juan)

delega vive en un repo personal. Con la configuración de hoy sus trabajos van al perfil `personal` y a `kindling-personal` (kling-ascci); trycorepc y dev-trycore solo aceptan el perfil `trycore`.

- **Ruta A, la que la configuración permite hoy**: `kindling-personal`, 3 VMs, cuenta personal. Hace falta el dorado `personal-dotfiles-ia` (no existe en `kindling.json`) y que kling-ascci **no** se actualice hasta el final del día.
- **Ruta B, la del encargo**: 3 trabajos en trycorepc y 1 en dev-trycore con `--profile trycore`. Lleva un repo personal a la cuenta y al PC de Trycore: solo con el OK explícito de Juan. Hay precedente (delega se construyó en trycorepc el 23-oct, rama `delega/w1-broker`). No se comprobó que `--profile trycore` pase la identidad desde esta carpeta.

Las olas valen para las dos rutas: 3 huecos. Los tiempos son de la ruta B; en la A hay que sumar ~30 % (herramientas más lentas) y 20 min del dorado.

### Reglas comunes

- Repo `dotfiles-ia`, carpeta `delega/`. Rama de trabajo `claude/orq-mejoras` salida de `main`; cada trabajo en `claude/orq-<tarea>`. Nada se empuja ni se fusiona en `main` sin el OK de Juan.
- Modelo Sonnet salvo los revisores (Opus, `--role reviewer --cap-usd 6`). Sin Fable.
- Criterio común de terminado: `cd delega && python3 -m unittest discover -s tests` en verde (hoy: 245 pruebas, 150 s; una pasada dio 1 fallo y la siguiente ninguno: hay una prueba inestable que T0 identifica y arregla), `ruff` limpio y pruebas nuevas que fallan sin el cambio.
- Las cadenas nuevas de `i18n/en.json` y `es.json` van con el prefijo del trabajo; es el único fichero compartido y el orquestador lo fusiona en el orden de la tabla.
- Entre olas integra el orquestador en el Mac: `git merge` de cada rama sobre `claude/orq-mejoras` y la suite. Son ramas locales.

### Olas

**Ola 0 (09:00–10:15)**

| Trabajo | Objetivo | Zona | Topes | Terminado cuando | Depende de |
|---|---|---|---|---|---|
| T0 fase 1 | Fusionar `claude/kling-fase1-b556de` sobre `main`, localizar y arreglar la prueba inestable de la suite y lo que rompa la fusión | `src/delega/{launch,queue,kindling,jobs,remote,config,__main__}.py`, `i18n/*`, `tests/test_{launch,kindling,execution,databases}.py`, `tests/fakes/**`, `skills/**`, `ARCHITECTURE.md` | 60 min · 6 USD · 300 turnos | Suite en verde; `delega run` con nodo lleno devuelve rc 6 y deja una entrada en la cola y ninguna carpeta (prueba existente de la rama) | — |
| T1 integrar | `delega integrate --merge --node <n>`: fusiona en la rama destino, corre `verify` en el nodo, escribe el resultado en `audit` y sale 0/7/8 | `src/delega/integrate.py`, `tests/test_ops_integrate_sync.py` | 60 min · 6 USD · 300 turnos | Prueba con el `ssh` falso: verde fusiona, rojo no deja la rama destino tocada, conflicto lista ficheros | — |
| T2 informe por ola | `delega metrics waves <repo>`: paralelismo, minutos con 0/1/2/3 workers, fase de trabajadores e integración por prefijo de rama | `src/delega/metrics.py`, `tests/test_ops_metrics_doctor.py` | 45 min · 4 USD · 200 turnos | Sobre una copia de estado de prueba reproduce las cifras de §3.2 (1,17; 24 y 21 min) | — |

10:15–10:40: el orquestador integra T0, T1 y T2. **Juan (10:30): OK a la fusión local de la fase 1** tras ver la suite.

**Ola 1 (10:40–11:50)**, sobre la fase 1

| Trabajo | Objetivo | Zona | Topes | Terminado cuando | Depende de |
|---|---|---|---|---|---|
| T3 bundle | `push_head`: no subir si HEAD es ancestro de la base; `HEAD --not merge-base` si divergen; partir por encima de 60 MB | `src/delega/kindling.py`, `tests/test_kindling.py`, `tests/fakes/kling-bin/kling` | 45 min · 4 USD · 200 turnos | Tres pruebas: HEAD por detrás (0 bytes), divergente (solo lo propio), bundle de 70 MB llega entero con el `kling` falso limitado a 64 MB | T0 |
| T4 rama en uso | Al lanzar sobre una rama cuyo worktree es de un trabajo con `END`, retirarlo y seguir; aviso si HEAD va por detrás de `origin`; aviso a la tercera vuelta sobre la misma rama | `src/delega/launch.py`, `src/delega/remote.py`, `src/delega/lint.py`, `tests/test_launch.py`, `tests/test_lint.py` | 60 min · 6 USD · 300 turnos | Prueba: relanzar una rama de un trabajo cerrado arranca; con el trabajo vivo sigue rechazando con rc 5 | T0 |
| T5 limpieza | `delega clean [--node n] [--older-than 3d] [--yes]`: lista y borra en nodos host los directorios de trabajos con `END` y rama traída; nunca uno vivo | `src/delega/clean.py` (nuevo), `tests/test_clean.py` (nuevo), una línea en `__main__.py` | 45 min · 4 USD · 200 turnos | Sin `--yes` solo lista; con `--yes` borra solo los cerrados; un trabajo con lease fresco no aparece | T0 |

En paralelo desde las 10:45, **R1 (Opus, solo lectura, `--cap-usd 6`, 30 min)**: revisa el diff de la ola 0 sobre `main`; busca rutas donde un trabajo quede abierto sin `END` o una VM sin dueño. Usa el cuarto hueco (dev-trycore en la ruta B; en la A espera a que acabe T3).

11:50–12:20: integración de la ola 1 y de lo que R1 marque como bloqueante (un trabajo de arreglo como máximo). 12:20–13:00: pausa y colchón.

**Ola 2 (13:00–14:40)**

| Trabajo | Objetivo | Zona | Topes | Terminado cuando | Depende de |
|---|---|---|---|---|---|
| T6 olas | `delega wave <repo> <ola.tsv> [--par N] [--into rama]`: lanza las filas (zonas disjuntas comprobadas antes de lanzar ninguna), espera por eventos del bus, fusiona las ramas ok, llama a `integrate --merge --node`, y solo si sale rojo lanza un trabajo de arreglo con el registro de la suite | `src/delega/wave.py` (nuevo), `tests/test_wave.py` (nuevo), `skills/delega/templates/*/ola.tsv`, una línea en `__main__.py` | 90 min · 10 USD · 500 turnos | Con los falsos: ola de 3 filas verde termina sin lanzar integrador; ola con una fila roja lanza exactamente un arreglo; dos zonas solapadas rechazan la ola entera antes de lanzar | T0, T1 |
| T7 puerta | Al cerrar el trabajo, el broker corre los `verify` del proyecto en el worktree y escribe `verify: ok|fail|none` y su duración en `summary.json`; `ps` lo muestra | `src/delega/broker.py`, `tests/test_broker.py`, `tests/fakes/stub_broker.py` | 60 min · 6 USD · 300 turnos | Prueba: `verify` que falla deja `ok` con `verify: fail` y un aviso `blocker`; sin `verify` configurado no cambia nada | T0 |

El tercer hueco queda libre para un arreglo de la revisión. 14:40–15:10: integración de la ola 2.

**Ola 3 (15:10–16:20)**

| Trabajo | Objetivo | Zona | Topes | Terminado cuando | Depende de |
|---|---|---|---|---|---|
| T8 piloto | Ola real de 3 filas con `delega wave` sobre el repo de pruebas `delega-dbdemo` (cambios triviales en ficheros distintos), con Haiku | repo `delega-dbdemo`; en `dotfiles-ia` solo lee | 3 × (10 min · 1 USD) | 0 `launch_failed`; menos de 10 s entre lanzamientos; integración verde sin agente; `delega metrics waves` da paralelismo ≥ 2,5 en la fase de trabajadores | T6, T2 |
| T9 documentación | `skills/delega/SKILL.md`, `ARCHITECTURE.md`, `CHANGELOG.md`: `wave`, `integrate --merge`, `clean`, rc 6, `verify` en el resumen | `skills/delega/**`, `ARCHITECTURE.md`, `CHANGELOG.md`, `README.md` | 45 min · 4 USD · 200 turnos | Cada comando nuevo aparece en la tabla de referencia y en los contratos de módulo; sin enlaces rotos | T6, T7 |
| R2 revisor | Opus, solo lectura: diff completo de `claude/orq-mejoras` contra `main`; un caso roto de referencia por mejora | — | 40 min · 6 USD | Informe con hallazgos clasificados; ninguno bloqueante sin arreglo o sin pasar a pendientes | Olas 0–2 |

16:20–17:15: arreglos de R2 (un trabajo), suite final, y el orquestador escribe el resumen. **Juan (17:15): OK de merge a `main` y de push**. Después, `delega sync` a los nodos y actualizar `orquestar-trycore` y `orquestar-personal` (otro repo, `~/dotfiles/private`): `max_jobs` = 3, `finde.sh` sustituido por `delega wave`.

### Pasos manuales de Juan

| Hora | Paso |
|---|---|
| 08:30 | Elegir ruta A o B. En la B: liberar disco en dev-trycore (hoy 13 GB libres, mínimo 15; 203 carpetas viejas) borrando a mano los trabajos cerrados, porque `delega clean` aún no existe. En la A: construir el dorado `personal-dotfiles-ia`. |
| 10:30 | OK a la fusión local de la fase 1. |
| 12:00 | Leer el informe de R1 y decidir qué bloquea. |
| 17:15 | OK de merge a `main` y push. |
| Después de las 18:00, opcional | Actualizar kling en kling-ascci y rehacer dorados. Fuera del camino del día; mejor otro día. |

### Coste, cupo y recortes

- Coste: suma de topes 73 USD (T0–T7 y T9: 50; R1 y R2: 12; T8: 3; dos arreglos previstos: 8). Estimación con las medianas medidas (Sonnet 1,14 USD por ok, Opus 2,88): **25–40 USD**. Cabe en los 150 USD del día.
- Cupo: unas 9 h de trabajador Sonnet repartidas en 7 h, como mucho 3 a la vez. Con la proporción medida (31 % de ventana con 1,18 workers) queda por debajo del 80 %. **Cabe en una cuenta.** Dos trabajos Opus de ≤ 40 min no cambian eso (el 29-oct hubo 14 trabajos Opus y la ventana quedó en 47 %).
- Reloj: 09:00 a 17:15, más la preparación de las 08:30: **8,75 h**.
- Si va con retraso, se recorta en este orden: T2 (informe), T7 (puerta), T3 (bundle), T9 pasa al día siguiente. No se recortan T0, T1, T4, T6 ni el piloto T8: son lo que acorta platform-fundations.
- Si T0 no queda verde a las 10:40, se para: todo lo demás se apoya en él.

### Qué no cubre este día

Actualizar kling-ascci, Vertex como segunda vía de capacidad, `contract_changed` y el empuje a los afectados (R8), el carril único de E2E (R6) y volver a medir los turnos vacíos del orquestador.

## 7. Preguntas abiertas para Juan

1. ¿Ruta A (cuenta personal, kindling-personal) o ruta B (cuenta y PC de Trycore) para trabajar sobre delega?
2. ¿Se acepta que `delega integrate --merge` fusione sin agente cuando la suite sale verde, y que el agente solo entre en rojo?
3. Con el pico de 5 h en 32 % a 1,17 workers, ¿se sube el frente de platform-fundations a 3–4 en una sola cuenta antes de pedir las otras dos, y se revisa el coste del plan (1.368 USD frente a ~0,6 USD por trabajo medido)?
4. ¿kling-ascci se actualiza en un hueco propio con los 10 dorados rehechos, o se deja en v0.18 hasta acabar platform-fundations?
5. ¿Se pone un freno duro a la tercera vuelta sobre una misma rama y a Fable sin motivo (hoy son 420,8 y 357,1 USD), o solo un aviso?

## Sin verificar

- La causa de los 32 `not_started` y de la ráfaga de rc 1 del 13-nov.
- Cuántas integraciones de platform-fundations necesitaron de verdad un arreglo del agente (de eso depende el ahorro de M2).
- Que la extrapolación lineal del cupo (3 workers ≈ 80 %) se cumpla; solo hay medida hasta 1,18 de media con picos de 3.
- Qué prueba de la suite es inestable en `main` en el Mac: de dos pasadas de 245 pruebas, la primera dio 1 fallo y la segunda ninguno; no se identificó cuál.
- El estado de kling-pve y del lab, y que la actualización de 0.18 a 1.0 conserve los dorados.
- No se ejecutaron `delega metrics report`, `delega spend` ni `chrono-ia delega`: las cifras salen de leer el estado directamente.

## 8. Cambio del 17-nov: memoria central y relevo del orquestador

**Por qué.** El fin de semana el orquestador del Mac (el general y el de proyecto, `finde.sh`) se quedó trabado y la orquestación se paró: nadie más sabía qué tocaba.

Así funciona hoy (COMPROBADO, `src/delega/queue.py`):
- La cola de delega es `queue.jsonl` en el `state_dir` del Mac, con `flock` local.
- Guarda `repo` y `brief` como **rutas absolutas del Mac** (`add()`). Ningún otro equipo podría lanzarlas aunque viera el fichero.
- Las olas de platform-fundations solo existían en un bash del Mac.

**Decisiones de Juan (17-nov):**
- SQLite en un servidor por organización.
- El relevo lo hace un daemon determinista; Claude entra solo cuando hay que decidir.
- T2, T7 y T9 salen del día para dejar sitio.

### Diseño

**Una base por organización de ws, nunca compartida:**
- `trycore` va en `dev-trycore:~/.local/state/delega/memoria.db`, la VM que está siempre encendida.
- `personal` (`normal`, `personal-plus` y `aura`) va en un servidor que elige Juan (pregunta 6).
- Nada de Trycore entra en la base personal, ni al revés. `ws contexto` decide la base, igual que hoy decide el perfil.

**Acceso por ssh a un CLI en el servidor**, no por un sistema de ficheros de red: SQLite sobre NFS corrompe.
- La forma es `ssh <servidor> delega mem <orden> --json`, con cada orden en `BEGIN IMMEDIATE`.
- En el propio servidor el acceso es directo.

**Tablas:**
- `flujos`: id, org, repo (remoto git, no ruta), rama destino, estado y plan.
- `tareas`: id, flujo, ola, **texto del brief**, zona, dependencias, `run_args`, estado, intentos, job_id, nodo y rama.
  - Los estados posibles son `pendiente`, `lanzada`, `ok`, `rojo`, `arreglo`, `bloqueada` y `hecha`.
  - El brief va dentro de la base para que cualquier máquina de la org pueda lanzarlo sin el Mac.
- `leases`: rol, dueño (host:pid), vence_en y latido.
- `eventos`: registro de solo añadir.

**Relevo:**
- `delega tick --daemon` corre en cada máquina de la org.
  - Trycore: Mac, trycorepc y dev-trycore.
  - Personal: Mac y el servidor personal.
- Cada 30 s intenta tomar la lease `orquestador:<org>`, que vence a los 90 s.
- El dueño reconcilia los trabajos y lanza las tareas listas si hay hueco.
- Al acabar una ola la integra con `integrate --merge --node` (T1). Si sale rojo, lanza **un** trabajo de arreglo.
- Si el dueño muere, otro toma la lease en ≤ 2 min y sigue desde la base.
- Los trabajadores ya lanzados no se enteran, porque corren en los nodos.

**Claude solo para decidir.**
- Entra con un segundo rojo en la misma tarea, un conflicto o zonas solapadas.
- En esos casos el daemon marca la tarea `bloqueada`. Luego abre un orquestador Claude en la misma máquina, con la cuenta de la org (`ws exec claude -p` y el contexto de la base). Si no puede, avisa por nod.
- Nunca hace merge a `main` ni push sin el OK de Juan.

**Si se cae el servidor de la base:**
- Los trabajos en curso siguen corriendo, pero no se lanza nada nuevo hasta que vuelva.
- Cada 5 min, `sqlite3 .backup` deja una copia en la máquina hermana de la org (de dev-trycore a trycorepc).
- `delega mem promote` convierte esa copia en la principal. Solo lo lanza Juan.

**Desaparece el orquestador de proyecto como proceso aparte.** `finde.sh` pasa a ser un `flujo` en la base, y `delega wave` (T6) solo escribe en ella.

### Olas del día, revisadas

Salen T2, T7 y T9; entran M1, M2 y M3.

| Ola | Trabajos | Nuevo |
|---|---|---|
| 0 (09:00–10:15) | T0 fase 1 · T1 integrar · **M1 memoria** | M1 |
| 1 (10:40–11:50) | T3 bundle · T4 rama en uso · T5 limpieza · **M2 lease y daemon** (4.º hueco, o justo después de T5) · R1 revisor | M2 |
| 2 (13:00–14:40) | T6 olas (escribe en la base) · **M3 relevo** | M3 |
| 3 (15:10–16:20) | T8 piloto con **prueba de caos** · R2 revisor | caos |

**M1 memoria** (sin dependencias; 60 min · 6 USD · 300 turnos)
- Objetivo:
  - `src/delega/mem.py` con el esquema SQLite.
  - Órdenes `delega mem init|add-flow|add-task|claim|heartbeat|release|next|set|log|export --json`, todas en `BEGIN IMMEDIATE`.
  - `queue add` escribe en la base, con el remoto git y el texto del brief en vez de rutas.
- Zona:
  - `src/delega/mem.py` y `tests/test_mem.py`, los dos nuevos.
  - En `queue.py`, solo `add` y `read_items`, detrás de una interfaz.
  - Una línea en `__main__.py`.
- Terminado cuando:
  - Si dos procesos reclaman la misma lease, gana uno y el otro recibe rc 9.
  - Una tarea añadida por un proceso la lee otro con `--json`.
  - Una ruta local en `repo` se rechaza.

**M2 lease y daemon** (depende de M1; 60 min · 6 USD · 300 turnos)
- Objetivo:
  - `tick --daemon --interval 30` toma o renueva `orquestador:<org>` y solo actúa si es el dueño.
  - Resuelve la base por `ws contexto` y la org, con una sección `[memoria.<org>] host=…, path=…` en la config.
  - `delega mem install-daemon` genera la unidad systemd `--user` y el plist de launchd.
- Zona: tick en `src/delega/queue.py`, la sección `memoria` en `src/delega/config.py` y `tests/test_tick_daemon.py` (nuevo).
- Terminado cuando:
  - Con dos daemons sobre la misma base, solo lanza uno.
  - Si muere el dueño, el otro toma la lease en menos de dos intervalos después de que venza.
  - Una base Trycore rechaza con rc 2 cualquier tarea con perfil personal.

**M3 relevo** (depende de M2 y de la interfaz de la base que usa T6; 90 min · 10 USD · 500 turnos)
- Objetivo, que un nodo pueda orquestar:
  - `delega sync` lleva al nodo la config del perfil y `delega.pyz`, sin ninguna credencial del Mac.
  - `tick` lanza desde el nodo usando el repo bare de `~/orquestador`.
  - Al segundo rojo, marca la tarea `bloqueada` y pide un orquestador Claude (`ws exec claude -p`, Sonnet, tope 3 USD) con el export de la base.
  - `.backup` cada 5 min a la máquina hermana.
- Zona:
  - `src/delega/sync.py` y `src/delega/launch.py` (de dónde sale el repo cuando no es local).
  - En `src/delega/mem.py`, backup y promote.
  - En `skills/delega/SKILL.md`, solo la sección de relevo.
  - `tests/test_relevo.py`, nuevo.
- Terminado cuando, con los `ssh` falsos:
  - Un daemon «en trycorepc» lanza una tarea cuyo brief solo está en la base.
  - El segundo rojo crea exactamente una petición de orquestador Claude.
  - `promote` exige `--yes`.

**Caos, dentro de T8** (depende de M3 y T6; 3 trabajos de 10 min y 1 USD)
- Piloto real de 3 tareas con `delega wave` sobre el repo `delega-dbdemo`. A mitad de la ola, Juan mata el daemon del Mac.
- Terminado cuando:
  - trycorepc toma la lease en ≤ 2 min.
  - Integra la ola sin el Mac.
  - `eventos` registra el cambio de dueño.
  - Hay 0 `launch_failed`.

**Zonas que se tocan:**
- M1 y T1 no comparten ficheros.
- M2 y T5 añaden una línea cada uno a `__main__.py`, y el orquestador las fusiona en orden.
- M3 y T6 se coordinan solo por la interfaz de `mem.py` que deja M1.

**Coste y reloj:**
- Salen 14 USD de topes (T2, T7 y T9) y entran 22 USD (M1, M2 y M3).
- La suma de topes sube a ~81 USD y la estimación queda en 30–45 USD.
- El reloj no cambia (8,75 h), pero se acaba el colchón. Si va con retraso, se recortan, en este orden, T3, T5 y R1.
- No se recortan T0, T1, M1, M2, M3, T6 ni la prueba de caos.

**Pasos manuales nuevos de Juan:**
- 08:30: elegir el servidor personal (pregunta 6) y liberar los 2 GB que faltan en dev-trycore, que pasa a guardar la base de Trycore.
- 15:30: matar el daemon del Mac durante la prueba de caos.
- Después del merge: `delega mem install-daemon` en cada máquina. Necesita su OK, porque es config persistente.

**Pregunta 6. ¿Dónde va la base personal?**
- (a) Un CT pequeño nuevo en pve. Siempre está encendido, pero es el mismo host que se ha colgado 4 veces.
- (b) kling-ascci, que ya corre kindling.
- (c) El Mac, con copia en otro equipo. Si el Mac se apaga, el relevo personal depende de esa copia.

Recomiendo (a), con copia al Mac.
