-- Rollback de 008_durable_conversation_processing.sql
--
-- Revierte únicamente lo agregado por esa migración: la tabla de cola, sus
-- índices, y las dos columnas nuevas en messages. No toca ventas, checkout,
-- pagos, órdenes, stock ni Knowledge, y no puede revertir nada de eso porque
-- 008 nunca los tocó.
--
-- CUÁNDO usar esto sin coordinar con nadie más:
--   * DURABLE_MESSAGE_PROCESSING_ENABLED nunca pasó a true en este entorno, o
--   * pasó a true pero no hay leases activos ni tráfico real en curso
--     (SELECT count(*) FROM conversation_processing WHERE lease_owner IS NOT
--     NULL da 0).
--
-- CUÁNDO NO correr esto sin avisar antes:
--   * si el flag está en true y hay workers corriendo: perderían la tabla y
--     las columnas a mitad de un ciclo de lease/generation, y cualquier
--     turno en vuelo fallaría al intentar leer o actualizar
--     conversation_processing.
--   * si ya se deployó el conversation_store.py que acompaña a esta migración:
--     record_isa_feedback() dejó de hacer un SELECT-luego-INSERT racy y ahora
--     inserta con "ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT
--     NULL DO NOTHING", apoyado en messages_wa_message_id_uidx. Ese INSERT
--     corre siempre que Isa manda feedback, sin importar
--     DURABLE_MESSAGE_PROCESSING_ENABLED. Si este rollback se corre con ese
--     código ya deployado, Postgres empieza a devolver "there is no unique or
--     exclusion constraint matching the ON CONFLICT specification" en cada
--     mensaje de Isa. Antes de correr este rollback en un entorno así, revertir
--     también ese fragmento de conversation_store.py (volver al
--     SELECT-luego-INSERT), o asumir que Isa se queda sin feedback hasta
--     resolverlo.
--
-- No borra mensajes ni conversaciones: sólo columnas/tablas creadas por 008.

BEGIN;

DROP TABLE IF EXISTS conversation_processing;

DROP INDEX IF EXISTS messages_claim_order_idx;
DROP INDEX IF EXISTS messages_wa_message_id_uidx;

ALTER TABLE messages
    DROP COLUMN IF EXISTS received_at,
    DROP COLUMN IF EXISTS provider_timestamp;

COMMIT;
