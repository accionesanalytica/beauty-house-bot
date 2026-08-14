-- Fred Core: la única fuente de verdad sobre el estado conversacional.
-- Ejecutar una sola vez en Supabase > SQL Editor.
--
-- Puramente aditivo: no modifica conversations, sales_intakes,
-- pending_actions, conversation_product_selections ni ninguna tabla
-- existente. No crea pedidos ni modifica stock.
--
-- Reemplaza (en el código) la reconstrucción de estado leyendo el texto del
-- último mensaje de Fred: cada conversación tiene un modo explícito y datos
-- estructurados, no inferidos.

CREATE TABLE IF NOT EXISTS fred_core_state (
    conversation_id BIGINT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    mode TEXT NOT NULL DEFAULT 'CHAT' CHECK (mode IN ('CHAT', 'MENU', 'CHECKOUT', 'TRACKING', 'ISA')),
    active_product_id TEXT,
    active_product_name TEXT,
    active_sku TEXT,
    active_variant TEXT,
    unit_price NUMERIC(12, 2),
    quantity INTEGER,
    delivery_method TEXT CHECK (delivery_method IN ('shipping', 'pickup')),
    customer_name TEXT,
    customer_email TEXT,
    postal_code TEXT,
    checkout_step TEXT,
    order_number TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fred_core_state_mode_idx ON fred_core_state (mode);
