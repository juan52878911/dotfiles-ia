---
name: web-moderna
description: Guías de la plataforma web moderna del equipo de Chrome (Modern Web Guidance, 187 guías), buscadas en local sin red. Usar al empezar a implementar UI web de cliente (HTML, CSS, JS en el navegador, también dentro de React, Vue o Angular), antes de escribir un componente o de añadir una dependencia para algo que la plataforma ya resuelve, cuando se trate de diálogos, popovers, menús, tooltips, acordeones, carruseles, formularios y validación, anchor positioning, container queries, `:has()`, view transitions, animaciones ligadas al scroll, modo oscuro, color, tipografía y fuentes de respaldo, scrollbars, rendimiento (LCP, INP, imágenes, content-visibility) o PWA. No usar para backend, CI, scripts locales ni para decidir el aspecto (eso es el DESIGN.md de plumb).
---

# Web moderna (Modern Web Guidance, en local)

Los modelos tienden a resolver con JavaScript y librerías cosas que hoy hace HTML o CSS (`<dialog>`,
`popover`, anchor positioning, `field-sizing`, view transitions…). Estas guías dicen qué API usar,
con qué soporte (Baseline) y con qué trampas. Vienen del repositorio de Chrome, fijado a una versión
en los dotfiles: se leen como ficheros, sin `npx`, sin red y sin telemetría.

`GUIAS` = `${CLAUDE_PLUGIN_ROOT}/skills/web-moderna/guides`

## Cómo buscar

1. **Por categoría:** las carpetas de `GUIAS` son `accessibility`, `built-in-ai`, `css`, `forms`,
   `html`, `js`, `performance`, `privacy`, `pwa`, `security`, `ui-atoms`, `ui-behaviors`,
   `ui-components`, `visual-design`, `wasm` y `webmcp`. Lista la que encaje (Glob `GUIAS/<categoría>/*.md`):
   el nombre del fichero describe el caso (`dark-mode.md`, `visually-stable-font-fallbacks.md`…).
2. **Por palabra:** Grep con `-il` sobre `GUIAS` con el término de la API o del caso, en inglés
   (`popover`, `anchor`, `view-transition`, `scroll-timeline`, `field-sizing`, `container`, `LCP`…).
   Los títulos (`# …`, primera línea) resumen cada guía.
3. **Lee la guía entera** que encaje (Read) antes de escribir código, y aplica sus avisos de
   soporte: si una API no es Baseline para los navegadores del proyecto, usa el respaldo que propone.

Lee una o dos guías por tarea, no todas: cada una cuesta entre 500 y 2.000 tokens.

## Con plumb

- El **DESIGN.md** decide los valores (colores, escala, radios, espaciado); estas guías deciden las
  **APIs** con las que se implementan. Si una guía trae colores o tamaños de ejemplo, usa los tokens.
- Después de implementar, mide con `plumb measure` y `plumb score` (contraste, foco visible,
  desborde, estados). Una guía no garantiza accesibilidad: revisiones externas encontraron ejemplos
  que no cumplían WCAG, así que lo que diga la medida manda sobre lo que diga la guía.

## Origen

Guías de [GoogleChrome/modern-web-guidance](https://github.com/GoogleChrome/modern-web-guidance)
(guías CC-BY 4.0, código Apache-2.0; ver `LICENSE`), mantenidas por Chrome, Edge, Firefox y MDN.
Esta skill sustituye a la suya, que llama a `npx -y modern-web-guidance@latest` en cada búsqueda.
Para actualizar las guías: cambia el commit del input `modern-web-guidance` en `~/dotfiles`.
