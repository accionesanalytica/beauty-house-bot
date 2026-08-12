# Knowledge V1 — implementación local

## Alcance

Knowledge V1 usa exclusivamente las cuatro fuentes recientes aprobadas por Isa:

1. seguimiento, retiros y escalaciones;
2. pestañas, lifting, adhesivos, aplicación y reutilización;
3. kits de retoque;
4. operación comercial, pagos, mayorista, showroom y postventa.

No usa chats históricos, Trello ni PDFs antiguos para completar hechos.

## Estructura

- `knowledge/facts/`: hechos estables o con vigencia explícita.
- `knowledge/procedures/`: pasos, límites y escalaciones.
- `knowledge/faq/`: respuestas breves derivadas de las mismas fuentes.
- `knowledge/playbook/`: comportamiento general, no indexado.
- `knowledge/dynamic-data-contracts.md`: frontera live, no indexada.

Cada documento indexado contiene frontmatter JSON validado con `topic`,
`knowledge_type`, fuente, aprobación, revisión, vigencia, riesgo y obligaciones.

## Circuito

```text
Markdown aprobado
  → metadata validada
  → chunks por sección
  → retrieval acotado
  → agrupación de metadata por topic
  → required_disclosures / required_links / routing
  → contexto del agente
  → respuesta del modelo
  → guardrail determinista de disclosures y URLs
```

El guardrail agrega disclosures o links obligatorios faltantes. Elimina URLs que
no sean una referencia estática aprobada ni un link dinámico entregado por una
herramienta live.

## STATIC vs DYNAMIC

No se indexan precio/stock actuales, precio o mínimo mayorista exactos,
checkout, tracking/estado actual, horarios disponibles, promociones/comisiones,
datos bancarios ni tarifa actual de courier. Esos datos requieren una tool live
o confirmación de Isa.

La regla estable “el mínimo mayorista depende del producto y habitualmente puede
ser 3, 6 o 12” sí forma parte de Knowledge; la cifra exacta de un producto no.

## Persistencia y activación controlada

`docs/sql/007_knowledge_metadata.sql` agrega `metadata JSONB` y su índice por
topic. Knowledge V1 contiene 42 chunks pertenecientes exclusivamente a 11
`source_id` aprobados por Isa. `api/index_knowledge.py` se niega a escribir si
esa columna no existe o si el corpus no coincide exactamente con esos límites.

La fuente se elige con `KNOWLEDGE_RAG_SOURCE`:

- `local`: Markdown revisado incluido en el repositorio. Es el valor seguro por
  defecto y el rollback inmediato.
- `supabase`: pgvector. Si la conexión o consulta falla, Fred vuelve a `local`
  para ese retrieval en lugar de responder con una base vacía.

Rollback: establecer `KNOWLEDGE_RAG_SOURCE=local` y redesplegar. No requiere
reindexar, borrar filas ni modificar tablas comerciales.

## Benchmark

- Casos: `evals/knowledge_v1_cases.jsonl`.
- Baseline: `evals/results/knowledge_v1_baseline.json`.
- Knowledge V1: `evals/results/knowledge_v1_after.json`.

Los casos multi-message de concurrencia, correcciones y cambios de producto se
registran como `UNSUPPORTED` porque pertenecen a orquestación, no a Knowledge.
No fueron “arreglados” dentro de esta fase.
