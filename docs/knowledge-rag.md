# Fase 3 — Knowledge RAG

## Propósito

El Knowledge RAG recupera conocimiento aprobado para preguntas generales:
políticas, cambios, encargos, preventa y cómo funciona el negocio. Es distinto
del RAG de catálogo: no identifica variantes ni responde stock/precio.

## Fuente de verdad y ciclo de vida

Los únicos documentos indexables viven en `knowledge/`. Cada archivo es
revisable por Git y se parte en chunks con fuente y sección. En Supabase cada
chunk queda como `draft`, `approved` o `retired`, y sólo los aprobados y activos
pueden llegar a Fred.

La primera fuente es `knowledge/politicas-operativas.md`. No se indexaron
chats crudos, documentos históricos ni información de pago: antes deben ser
curados, fechados y aprobados.

## Seguridad comercial

El contexto recuperado trae su propia advertencia: no puede confirmar stock,
precio, pago, dirección, plazos concretos ni estado de pedido. Para eso Fred
usa Tiendanube o escala a Isa.

## Activación posterior

1. Ejecutar `docs/sql/004_knowledge_chunks.sql` manualmente en Supabase.
2. Revisar `python api/index_knowledge.py` (simulación sin costo).
3. Con aprobación explícita, ejecutar `python api/index_knowledge.py --apply`.
   Ese paso llama Gemini para generar embeddings.
4. Agregar `KNOWLEDGE_RAG_ENABLED=true` en Railway y desplegar.

Mientras la variable sea `false` (el valor por defecto), Fred no consulta la
tabla ni cambia su comportamiento. Cuando esté activo, catálogo y conocimiento
comparten el mismo embedding de la consulta para evitar una llamada duplicada.
