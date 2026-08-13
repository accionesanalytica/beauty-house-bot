-- M1: recepción durable y procesamiento exclusivo por conversación.
--
-- Esta migración sólo amplía el historial de mensajes y agrega una cola de
-- coordinación. No toca ventas, checkout, pagos, órdenes, stock ni Knowledge.
--
-- Verificado con un preflight de SOLO LECTURA contra Postgres real
-- (2026-08-12), antes de escribir esta versión:
--   * messages: 197 filas, 0 duplicados de wa_message_id.
--   * provider_timestamp / received_at / conversation_processing: no existen
--     todavía (instalación limpia, nada parcialmente aplicado).
--   * conversation_id=1 (clienta) tiene 98 mensajes in/customer históricos
--     (ids 1..196) anteriores a esta migración. La cola durable seguiría
--     tratándolos como pendientes si no fuera por el seed de
--     last_processed_message_id que ya implementa
--     conversation_store.enqueue_inbound_message.
--   * conversation_id=4 (Isa) es una conversación aparte: sus mensajes nunca
--     entran a esta cola porque _ingest_durable_webhook() los deriva a
--     handle_isa_message() antes de llamar a enqueue_inbound_message().
--
-- Rollback: docs/sql/008_durable_conversation_processing_rollback.sql
-- Sólo usar ese rollback si DURABLE_MESSAGE_PROCESSING_ENABLED nunca pasó a
-- true en el entorno donde se aplicó (ver ese archivo para el detalle).

BEGIN;

-- provider_timestamp: sólo lo completan los mensajes que llegan por la ruta
-- durable (extract_inbound_messages lee message["timestamp"] de Meta). No
-- tiene equivalente previo, por eso es nullable sin backfill.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS provider_timestamp TIMESTAMPTZ;

-- received_at: se agrega nullable primero para poder backfillear las filas
-- históricas con su created_at real antes de exigir NOT NULL. Si se agregara
-- directo como NOT NULL DEFAULT now(), Postgres evalúa ese DEFAULT una sola
-- vez para todas las filas existentes en el momento del ALTER, y las 197
-- filas actuales quedarían con received_at = instante de la migración en vez
-- de su fecha real de ingreso.
--
-- Se mantiene como columna propia en vez de reutilizar created_at: el resto
-- del código (panel interno, load_history, snapshots de observabilidad) ya
-- lee created_at con su propio significado, y el patrón "cada subsistema
-- tiene su propio timestamp de ingreso" ya existe en operations_store.py
-- (tiendanube_webhook_events.received_at vs fred_turn_observations.
-- created_at). Reusar created_at acá acoplaría la cola durable a una columna
-- que otras funcionalidades ya leen para otros fines, y hubiera exigido
-- reabrir conversation_store.py fuera del alcance de esta migración. Hoy
-- ambas columnas quedan iguales para toda fila existente (mismo backfill);
-- a futuro pueden divergir sin arrastrar consecuencias a otras pantallas.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;

UPDATE messages
SET received_at = created_at
WHERE received_at IS NULL;

ALTER TABLE messages
    ALTER COLUMN received_at SET DEFAULT now(),
    ALTER COLUMN received_at SET NOT NULL;

-- Necesario para que "INSERT ... ON CONFLICT (wa_message_id) WHERE
-- wa_message_id IS NOT NULL DO NOTHING" sea una deduplicación atómica real
-- (ya no una carrera SELECT-luego-INSERT). El preflight confirmó 0
-- duplicados existentes, así que este índice no puede fallar al crearse.
CREATE UNIQUE INDEX IF NOT EXISTS messages_wa_message_id_uidx
    ON messages (wa_message_id)
    WHERE wa_message_id IS NOT NULL;

-- Índice pensado para la query real de claim_next_conversation(): filtra por
-- conversation_id + direction/sender de clienta, y ordena por
-- COALESCE(provider_timestamp, received_at). La versión anterior de esta
-- migración creaba un índice genérico (conversation_id, received_at, id) que
-- ninguna query usaba tal cual (la query real ordena por una expresión
-- COALESCE, no por received_at solo).
CREATE INDEX IF NOT EXISTS messages_claim_order_idx
    ON messages (conversation_id, COALESCE(provider_timestamp, received_at), id)
    WHERE direction = 'in' AND sender = 'customer';

CREATE TABLE IF NOT EXISTS conversation_processing (
    conversation_id BIGINT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    first_pending_at TIMESTAMPTZ,
    process_after TIMESTAMPTZ,
    latest_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    last_processed_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    last_delivered_message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (generation >= 0)
);

CREATE INDEX IF NOT EXISTS conversation_processing_ready_idx
    ON conversation_processing (process_after)
    WHERE process_after IS NOT NULL;

CREATE INDEX IF NOT EXISTS conversation_processing_lease_idx
    ON conversation_processing (lease_until)
    WHERE lease_owner IS NOT NULL;

COMMIT;
