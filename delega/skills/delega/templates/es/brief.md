# <Pieza>: <objetivo en una frase>

Encargo: `<ruta del encargo.md>` (criterio de aceptación congelado; no lo cambies tú).

## Hipótesis
- Qué crees que pasa y **cómo refutarlo rápido** (primero sin cómputo de pago: datos guardados, tests, A/B determinista).

## Terminado cuando
- Criterio numérico o comprobable: comando + salida esperada, test que pasa, métrica ≥ umbral.
- Criterio de parada: si tras 2 intentos no mejora, para y avisa con `blocker` (es «limitación conocida»).

## Zona
- Ficheros/carpetas que tocas. **No tocar**: …
- Reglas del CLAUDE.md del repo. Rama `<prefijo permitido por los hooks>/<nombre>`; commitea; sin push a main ni merge.

## Entorno
- Puertos/contenedores con nombre propio (`<pieza>-pg`…); al terminar, desmonta lo que hayas levantado.
- Línea base: `<sha o rama única>` — la misma para todas las máquinas.

## Tope
- ≤ <N> min de reloj · cómputo de pago: <0 | N llamadas / ≤ X USD> — antes de cada ronda viva, avisa con `milestone` cuántas llamadas hará.
- Sin subagentes salvo <…> (cada subagente multiplica el coste).

## Informe
Termina con un informe de ≤15 líneas: qué cambiaste, rama y sha, qué verificaste
ejecutando (con números), qué quedó sin verificar y qué recomiendas a continuación.
