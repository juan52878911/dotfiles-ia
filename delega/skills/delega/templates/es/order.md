# Encargo: <nombre>

> Se escribe y se acuerda con {owner} **antes** del primer trabajador. Después solo se toca
> por la sección «Cambios», con fecha, motivo y el visto bueno de {owner}.

## Objetivo y entregable
- Qué existe al final que hoy no existe (PR, informe, métrica). Uno solo.

## Criterio de aceptación (congelado)
- Por caso/contenido: entrada → salida esperada. Rúbrica única, denominadores fijos.
- Hash de las expectativas: `shasum -a 256 <fichero>` = `…`
- «No empeorar la base»: juzgado con 3 corridas (mín/media/máx), no con una.

## Línea base
- Sha/rama única: `…`, medida una vez con la misma configuración que las rondas finales.

## Presupuesto
- Reloj: ≤ … h · gasto de la cuenta: ≤ … $ (`delega spend`) · cómputo externo de pago: ≤ … USD.
- Por pieza: ≤ 2 h y ≤ 10 $. Revisión a mitad: si va > 60 % del presupuesto con < 50 % hecho, se para y se replantea con {owner}.

## Fuera de alcance
- Lo que NO entra (funcionalidades nuevas, infra, otros repos). Lo que aparezca va a issues.

## Decisiones que son de {owner}
- Lista de lo que el orquestador no decide solo (producto, criterios, gasto extra, datos de clientes).

## Cambios
| Fecha | Qué cambió | Por qué | OK de {owner} |
|---|---|---|---|
