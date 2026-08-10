-- Fase 3: base de conocimiento versionada y recuperable.
-- Ejecutar manualmente en Supabase antes de `api/index_knowledge.py --apply`.
-- Esta migración no modifica catálogo, órdenes ni conversaciones.

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id BIGSERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    section TEXT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(768) NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'retired')),
    active BOOLEAN NOT NULL DEFAULT false,
    reviewed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx
ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 50);

CREATE INDEX IF NOT EXISTS knowledge_chunks_active_idx
ON knowledge_chunks (active, status);
