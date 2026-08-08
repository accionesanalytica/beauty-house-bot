# Bot WhatsApp para Tiendanube de Isa

## Objetivo
Automatizar atención al cliente y gestión de pedidos del ecommerce de Isa en Tiendanube.

## Alcance funcional
1. Bot de WhatsApp que responde FAQs (horarios, envíos, cambios/devoluciones, talles/stock)
2. Creación de pedidos vía API de Tiendanube
3. Impresión de etiquetas de envío vía RPA
4. Modelo de monetización: fee de setup inicial + retainer mensual (para eventualmente ofrecerlo a otros comercios)

## Premisa clave
Antes de tocar cualquier cosa técnica hay que ordenar la operación en Tiendanube: saber qué stock hay realmente, cruzarlo contra el sistema, y definir las reglas de negocio (tags, estados) que el bot y el sincronizador van a usar después. Programar sobre datos/reglas sucias es la forma más rápida de tener que rehacer todo.

## Estilo de trabajo
- Español rioplatense, directo, sin vueltas
- Antes de programar, mostrar el plan y preguntar dudas
- Priorizar simplicidad operativa sobre funcionalidades extra
- Cada decisión técnica se documenta en `/docs` para no perder contexto

## Estructura de carpetas

```
/docs     → decisiones técnicas, research de FAQs, pricing
/api      → integración con API de Tiendanube (pedidos, stock)
/rpa      → automatización de impresión de etiquetas (Mercado Envíos u otro)
/bot      → lógica del bot de WhatsApp (mensajería, flujos, respuestas)
/assets   → referencias a archivos de Isa (Excel finanzas, CSV ventas, PDFs de FAQs) — no versionar los originales, solo copias de trabajo o notas de dónde están
/tests    → pruebas de cada módulo (FAQs, API, RPA)
```

## Plan de trabajo por fases

### Fase 0 — Conteo físico + reglas de negocio en Tiendanube (prerequisito)
Esta fase es la base de todo lo demás — no se arranca con nada técnico hasta cerrarla.
1. **Conteo físico de stock** en un Excel nuevo, separado del workbook de finanzas existente (`/assets/conteo-fisico.xlsx`, a crear cuando arranque esta fase).
2. **Cruce contra Tiendanube**: comparar el conteo físico contra lo que dice el sistema → identificar el delta real (faltantes, sobrantes, productos mal cargados).
3. **Definición de reglas de negocio en Tiendanube**, en sesión conjunta con Isa (no las define Claude solo): por ejemplo el tag para "hecho por encargo" vs. stock real disponible, y cualquier otro estado/tag que se necesite para que el sincronizador pueda operar después sin ambigüedad.
- Entregable: `/docs/reglas-tiendanube.md` (primer borrador ya armado con export real de Tiendanube — falta validación final de Isa) + `/assets/conteo-fisico.xlsx`.
- **Hallazgo del export de productos (1507 filas):** 505 productos/variantes sin control de stock (campo vacío) y 490 con stock=0 pero publicados. Confirma que el problema no es solo falta de un tag de "encargo" — hay falta de disciplina de carga de stock en general.
- **Bloqueante:** las fases 1 y 2 dependen de que estas reglas estén definidas y el stock esté verificado.

### Fase 1 — Mapeo de FAQs
- Relevar FAQs desde cero: no hay lista armada todavía.
- Insumos a usar:
  - Export de chats de WhatsApp de Isa con clientes (para detectar preguntas recurrentes reales).
  - PDFs que Isa ya comparte con sus clientes (políticas de envío, cambios, talles).
- Entregable: `/docs/faqs.md` con preguntas + respuestas categorizadas (horarios, envíos, cambios/devoluciones, talles/stock).
- **Pendiente:** conseguir el export de chats y los PDFs de Isa.

### Fase 2 — Integración API Tiendanube
- Requiere cuenta de desarrollador / API key de Tiendanube (todavía no la tenemos).
- Alcance: creación de pedidos, consulta de stock.
- Entregable: módulo en `/api` + doc de auth y endpoints usados en `/docs/api-tiendanube.md`.
- **Pendiente:** dar de alta la app/API key en Tiendanube.

### Fase 3 — Impresión de etiquetas (RPA)
- Hipótesis de trabajo: Mercado Envíos (a confirmar).
- Definir si hay API disponible o si es necesario RPA por falta de integración directa.
- Entregable: script/flujo en `/rpa` + doc de la decisión (por qué RPA y no API) en `/docs/decisiones-tecnicas.md`.
- **Pendiente:** confirmar transportista/plataforma real.

### Fase 4 — Testing
- Pruebas de cada módulo por separado (FAQs, API, RPA) antes de integrar el flujo completo.
- Entregable: casos de prueba en `/tests` + checklist de QA manual para el flujo end-to-end del bot.

## Decisiones técnicas pendientes
Ver `/docs/decisiones-tecnicas.md` — incluye stack de WhatsApp, lenguaje, plataforma de envíos y pricing.

## Estado actual (al iniciar el proyecto)
- Existe workbook de Excel con las finanzas de Isa (multi-hoja) — no cargado al repo aún.
- Existe análisis de ventas de Tiendanube (CSV) con detección de anomalías — no cargado al repo aún.
- El concepto del bot está definido, sin código armado todavía.
