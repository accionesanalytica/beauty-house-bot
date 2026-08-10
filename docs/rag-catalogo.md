# Fase 2 — RAG de catálogo v2

## Qué resuelve

RAG (*Retrieval-Augmented Generation*, generación aumentada con búsqueda) le
entrega a Fred posibles identidades de producto antes de que responda. No es
una fuente de verdad comercial: Tiendanube mantiene el precio, el stock y la
publicación vigentes.

La búsqueda nueva combina dos señales:

1. **Léxica:** cada término identificador de la consulta debe aparecer en
   nombre, variante o SKU de una variante publicada. Esto prioriza pedidos
   como “Isabel chocolate” o un SKU.
2. **Semántica:** recupera más vecinas vectoriales que antes, pero sólo deja
   pasar las que superen la similitud mínima `0.62`.

Las coincidencias léxicas se muestran primero. Así se reduce el riesgo de que
una recomendación genérica (por ejemplo un producto sorpresa) gane contra una
variante que la clienta nombró explícitamente.

## Qué se indexa hoy

El CSV actual contiene nombre, variante, SKU, código de barras y `handle`.
Esos campos forman el texto de identidad del embedding. No se indexan stock
ni precio, aunque aparezcan en el CSV: cambian demasiado y se consultan en
Tiendanube al momento de responder.

El indexador también admite de forma futura `brand`, `category` y
`description`, pero el exportador actual todavía no los produce. Agregarlos
será un paso explícito de Fase 3, junto con políticas, guías y conocimiento
del negocio.

## Qué NO hicimos todavía

- No se ejecutó `api/index_catalog.py --apply`.
- No se llamó Gemini ni Supabase con datos reales.
- No se modificó Tiendanube.
- No hay reranking por otro modelo ni búsqueda híbrida por descripciones:
  antes hace falta enriquecer el catálogo y medir resultados reales.

## Próxima activación controlada

Después de revisar los tests y decidir que corresponde actualizar el índice,
la operación será: exportar catálogo fresco, revisar el dry run y recién ahí
ejecutar la indexación real. Esa acción usa embeddings de Gemini y se hará con
autorización explícita.
