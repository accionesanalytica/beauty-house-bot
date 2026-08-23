-- Fred v2 live: handoffs por tema y eventos operativos sin transcriptos ni PII.
-- Aplicar explícitamente antes de configurar BOT_RESPONSE_MODE=v2.
-- Es aditiva: agrega columnas/índices, sin borrar rutas, pedidos, stock ni datos de v1.

BEGIN;

-- Outbox mínimo: la respuesta se persiste antes de llamar a Meta y sólo entra
-- al historial visible cuando su estado llega a sent.
ALTER TABLE public.messages
    ADD COLUMN IF NOT EXISTS fred_v2_correlation_id TEXT,
    ADD COLUMN IF NOT EXISTS fred_v2_delivery_status TEXT,
    ADD COLUMN IF NOT EXISTS fred_v2_delivered_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS fred_v2_handoff_operation TEXT,
    ADD COLUMN IF NOT EXISTS fred_v2_handoff_id BIGINT,
    ADD COLUMN IF NOT EXISTS fred_v2_handoff_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messages_fred_v2_delivery_status_check'
          AND conrelid = 'public.messages'::regclass
    ) THEN
        ALTER TABLE public.messages
            ADD CONSTRAINT messages_fred_v2_delivery_status_check
            CHECK (
                fred_v2_delivery_status IS NULL
                OR fred_v2_delivery_status IN ('prepared', 'sent', 'failed')
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messages_fred_v2_handoff_action_check'
          AND conrelid = 'public.messages'::regclass
    ) THEN
        ALTER TABLE public.messages
            ADD CONSTRAINT messages_fred_v2_handoff_action_check
            CHECK (
                (
                    fred_v2_handoff_operation IS NULL
                    AND fred_v2_handoff_id IS NULL
                    AND fred_v2_handoff_reason IS NULL
                ) OR (
                    fred_v2_handoff_operation IS NOT NULL
                    AND fred_v2_handoff_id > 0
                    AND fred_v2_handoff_reason IS NOT NULL
                )
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messages_fred_v2_handoff_operation_check'
          AND conrelid = 'public.messages'::regclass
    ) THEN
        ALTER TABLE public.messages
            ADD CONSTRAINT messages_fred_v2_handoff_operation_check
            CHECK (
                fred_v2_handoff_operation IS NULL
                OR fred_v2_handoff_operation IN ('create', 'repeat', 'resolve')
            );
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'messages_fred_v2_handoff_reason_check'
          AND conrelid = 'public.messages'::regclass
    ) THEN
        ALTER TABLE public.messages
            ADD CONSTRAINT messages_fred_v2_handoff_reason_check
            CHECK (
                fred_v2_handoff_reason IS NULL
                OR fred_v2_handoff_reason IN (
                    'custom_order', 'human_request', 'purchase_intent', 'product_advice',
                    'unable_to_verify', 'cancel_order', 'modify_order', 'return_order',
                    'sensitive_order_action', 'operational_detail_unverified'
                )
            );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS messages_fred_v2_correlation_uidx
ON public.messages (fred_v2_correlation_id)
WHERE fred_v2_correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.fred_v2_handoffs (
    id BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL UNIQUE,
    conversation_id BIGINT NOT NULL REFERENCES public.conversations(id) ON DELETE CASCADE,
    reason TEXT NOT NULL CHECK (reason IN (
        'custom_order', 'human_request', 'purchase_intent', 'product_advice',
        'unable_to_verify', 'cancel_order', 'modify_order', 'return_order',
        'sensitive_order_action', 'operational_detail_unverified'
    )),
    order_number TEXT,
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared', 'pending', 'delivery_failed', 'customer_acknowledged')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.fred_v2_handoffs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.fred_v2_handoffs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.fred_v2_handoffs_id_seq FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS fred_v2_handoffs_conversation_status_idx
ON public.fred_v2_handoffs (conversation_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.fred_v2_events (
    id BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    event_index INTEGER NOT NULL DEFAULT 0 CHECK (event_index >= 0),
    conversation_id BIGINT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'response', 'tool', 'handoff', 'handoff_resolved', 'error'
    )),
    topic TEXT CHECK (topic IN ('general', 'knowledge', 'order', 'product', 'handoff')),
    outcome TEXT,
    tool_name TEXT CHECK (tool_name IS NULL OR tool_name IN (
        'search_knowledge', 'get_order', 'get_product', 'handoff_to_isa', 'unknown'
    )),
    handoff_reason TEXT,
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    llm_calls INTEGER CHECK (llm_calls IS NULL OR llm_calls >= 0),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    completion_tokens INTEGER CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
    error_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (correlation_id, event_type, event_index)
);

ALTER TABLE public.fred_v2_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.fred_v2_events FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.fred_v2_events_id_seq FROM PUBLIC, anon, authenticated;

CREATE INDEX IF NOT EXISTS fred_v2_events_created_idx
ON public.fred_v2_events (created_at DESC);

CREATE INDEX IF NOT EXISTS fred_v2_events_type_created_idx
ON public.fred_v2_events (event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS fred_v2_events_conversation_created_idx
ON public.fred_v2_events (conversation_id, created_at DESC);

COMMIT;
