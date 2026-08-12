-- Migración aplicada para Knowledge V1 el 2026-08-11.
-- Conserva metadata validada y permite agrupar obligaciones por topic.

ALTER TABLE knowledge_chunks
ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS knowledge_chunks_topic_idx
ON knowledge_chunks ((metadata ->> 'topic'));

COMMENT ON COLUMN knowledge_chunks.metadata IS
'Metadata curada: topic, knowledge_type, aprobación, vigencia y obligaciones.';
