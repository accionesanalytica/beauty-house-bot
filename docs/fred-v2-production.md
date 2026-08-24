# Fred v2 como responder único

Este documento describe el cutover implementado en la rama `fred-v2-agent`.
No autoriza ni ejecuta deploys, cambios en Railway ni migraciones remotas.

## Cableado del webhook

Con `BOT_RESPONSE_MODE=v2`, `_process_webhook_body` conserva la ingestión,
deduplicación, historial, agrupación de mensajes y lease durable existentes.
Después de esos controles llama `run_v2_customer_turn` y retorna. Por lo tanto,
no entra al ownership global, Fred Core, routing/retrieval de v1, shadow ni
`agent.answer`.

El runtime construye `FredV2Agent` con `V2ToolAdapters`. Guarda primero la
respuesta en un outbox mínimo dentro de `messages`, usa el sender existente y
sólo la vuelve visible al historial cuando WhatsApp confirmó el envío. No hay
fallback a v1.

## Handoff por tema

`handoff_to_isa` admite tres operaciones semánticas:

- `create`: prepara el estado y genera número visible + link `wa.me` dinámico;
- `repeat`: vuelve a mostrar el acceso y aclara que Fred no ve el chat con Isa;
- `resolve`: prepara el cierre únicamente de ese handoff cuando la persona confirma
  que ya habló; el estado se actualiza junto con la confirmación de entrega.

El estado vive en `fred_v2_handoffs`; nunca cambia `conversations.state` ni manda
mensajes a Isa. Un handoff nuevo queda `prepared` durante el turno y pasa a
`pending` sólo si el link fue entregado a la clienta. Un fallo de envío queda
`delivery_failed`. Sólo persiste motivo canónico, número de pedido opcional,
estado y timestamps; no guarda resumen libre ni transcript. El texto del link
se arma de forma canónica con esos datos. La migración habilita Row Level
Security (RLS) y revoca acceso a `PUBLIC`, `anon` y `authenticated`: ambas
tablas nuevas son sólo para el backend.

## Outbox e idempotencia

La migración agrega a `messages` una correlación opaca, estado
`prepared|sent|failed` y una acción de handoff limitada a operación/id/motivo.
La respuesta se guarda antes de llamar a Meta. Al confirmar el envío, el outbox
y el cambio de estado del handoff se actualizan en una sola transacción. Un
retry `prepared/failed` reutiliza el texto y la acción originales sin volver a
ejecutar el modelo; un retry `sent` no vuelve a enviar. También se recupera un
handoff que hubiera quedado preparado justo antes de una caída del proceso. En
la ruta síncrona, un retry duplicado de Meta entra sólo a esta recuperación de
outbox; si no existe respuesta/handoff recuperable, no ejecuta nuevamente el
modelo.

## Preparación antes del futuro deploy

1. Aplicar manualmente `docs/sql/012_fred_v2_cutover.sql` en el Postgres correcto.
   Antes, probar la migración y los cambios de estado en una base aislada.
2. Configurar en Railway:

```text
BOT_RESPONSE_MODE=v2
FRED_V2_SHADOW_ENABLED=false
ISA_WHATSAPP_NUMBER=5491124528750
ISA_INTERNAL_OPERATOR_NUMBER=
SUPABASE_DB_URL=<existente>
DEEPSEEK_API_KEY=<existente>
FRED_V2_MODEL=deepseek-chat
KNOWLEDGE_RAG_ENABLED=true
KNOWLEDGE_RAG_SOURCE=local|supabase
SALES_INTAKE_ENABLED=false
TIENDANUBE_CHECKOUT_MODE=disabled
DURABLE_MESSAGE_PROCESSING_ENABLED=false
FRED_CUSTOMER_MODE=open
```

`FRED_V2_MODEL_URL` es opcional y conserva el endpoint de DeepSeek por defecto.
Las credenciales read-only de Tiendanube/Knowledge/WhatsApp existentes se
mantienen. No configurar shadow junto con el responder live.

## Consultas de 30 días

Conversaciones atendidas y respuestas efectivamente enviadas:

```sql
SELECT
  COUNT(DISTINCT conversation_id) AS conversations,
  COUNT(*) AS responses
FROM fred_v2_events
WHERE event_type = 'response'
  AND outcome = 'sent'
  AND created_at >= now() - interval '30 days';
```

Conversaciones con respuesta y sin ningún handoff entregado en la ventana:

```sql
WITH attended AS (
  SELECT DISTINCT conversation_id
  FROM fred_v2_events
  WHERE event_type = 'response' AND outcome = 'sent'
    AND created_at >= now() - interval '30 days'
), handed_off AS (
  SELECT DISTINCT conversation_id
  FROM fred_v2_events
  WHERE event_type = 'handoff'
    AND created_at >= now() - interval '30 days'
)
SELECT COUNT(*)
FROM attended a
LEFT JOIN handed_off h USING (conversation_id)
WHERE h.conversation_id IS NULL;
```

Handoffs por motivo:

```sql
SELECT handoff_reason, COUNT(*)
FROM fred_v2_events
WHERE event_type = 'handoff'
  AND created_at >= now() - interval '30 days'
GROUP BY handoff_reason
ORDER BY COUNT(*) DESC;
```

Consultas por tema, Knowledge y herramientas:

```sql
SELECT topic, COUNT(*)
FROM fred_v2_events
WHERE event_type = 'response' AND outcome = 'sent'
  AND created_at >= now() - interval '30 days'
GROUP BY topic ORDER BY COUNT(*) DESC;

SELECT tool_name, COUNT(*)
FROM fred_v2_events
WHERE event_type = 'tool'
  AND created_at >= now() - interval '30 days'
GROUP BY tool_name ORDER BY COUNT(*) DESC;
```

Errores, latencia, llamadas al modelo y tokens:

```sql
SELECT error_type, COUNT(*)
FROM fred_v2_events
WHERE event_type = 'error'
  AND created_at >= now() - interval '30 days'
GROUP BY error_type ORDER BY COUNT(*) DESC;

SELECT
  ROUND(AVG(latency_ms)) AS avg_latency_ms,
  ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_latency_ms,
  ROUND(AVG(llm_calls), 2) AS avg_llm_calls,
  ROUND(AVG(prompt_tokens + completion_tokens), 2) AS avg_total_tokens
FROM fred_v2_events
WHERE event_type = 'response'
  AND outcome = 'sent'
  AND created_at >= now() - interval '30 days';
```

## Límites de la medición

Un evento `handoff` confirma que Fred entregó el link, no que la persona hizo
clic, escribió o acordó algo con Isa. `customer_acknowledged` sólo significa
que la persona dijo haber hablado. Además, “sin handoff” es una métrica por
ventana temporal; hoy no existe un evento formal de cierre de conversación.
Los temas se derivan de herramientas/handoffs semánticos: una pregunta que aún
no dispara herramienta (por ejemplo, pedido sin número) puede quedar como
`general`. La telemetría es fail-open: una caída de Postgres no bloquea la
respuesta, pero ese turno puede no quedar medido. Los handoffs de producto
persisten el motivo, no texto libre de producto/variante/cantidad, por lo que un
acceso repetido puede tener un contexto más general que el primer turno.

La cola durable permanece desactivada por defecto y no debe habilitarse hasta
validar las migraciones `008` y `012` contra Postgres real. El outbox evita
reejecutar el modelo y suprime reenvíos una vez que `sent` quedó confirmado,
pero ningún cliente HTTP puede eliminar por completo la ventana extrema en la
que Meta acepta el mensaje y el proceso cae antes del commit local; ese caso
podría reintentar el mismo texto. Los tests locales no sustituyen un smoke test
real de Postgres/Meta previo a activar el bot.
