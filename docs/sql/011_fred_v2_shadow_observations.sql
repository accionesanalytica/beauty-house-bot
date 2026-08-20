-- Fred v2 shadow: observabilidad aislada y sin efectos operativos.
-- Aplicar antes de activar FRED_V2_SHADOW_ENABLED; no se aplica automáticamente.

CREATE TABLE IF NOT EXISTS fred_v2_shadow_observations (
    correlation_id TEXT PRIMARY KEY,
    source_message_id_hash TEXT,
    conversation_id BIGINT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    input_text_redacted TEXT NOT NULL DEFAULT '',
    v1_response_redacted TEXT NOT NULL DEFAULT '',
    v2_response_redacted TEXT NOT NULL DEFAULT '',
    v1_response_hash TEXT NOT NULL DEFAULT '',
    v2_response_hash TEXT NOT NULL DEFAULT '',
    v2_tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    v2_tool_results JSONB NOT NULL DEFAULT '[]'::jsonb,
    v2_llm_calls INTEGER NOT NULL DEFAULT 0,
    v2_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    v2_completion_tokens INTEGER NOT NULL DEFAULT 0,
    v2_latency_ms INTEGER NOT NULL DEFAULT 0,
    v2_handoff_reason TEXT,
    error_type TEXT,
    side_effects BOOLEAN NOT NULL DEFAULT false CHECK (side_effects = false),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fred_v2_shadow_observations_created_idx
ON fred_v2_shadow_observations (created_at DESC);
