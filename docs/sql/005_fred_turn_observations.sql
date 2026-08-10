-- Fase 7: observabilidad segura de los turnos atendidos por el agente.
-- No almacena teléfonos, texto de clientas, respuestas completas ni credenciales.

CREATE TABLE IF NOT EXISTS fred_turn_observations (
    id BIGSERIAL PRIMARY KEY,
    source_message_id TEXT UNIQUE,
    conversation_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL,
    tool_names JSONB NOT NULL DEFAULT '[]'::jsonb,
    catalog_context_used BOOLEAN NOT NULL DEFAULT false,
    knowledge_context_used BOOLEAN NOT NULL DEFAULT false,
    model_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fred_turn_observations_created_idx
ON fred_turn_observations (created_at DESC);
