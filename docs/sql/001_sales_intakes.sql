-- Ficha de venta de Fred.
-- Ejecutar una sola vez en Supabase > SQL Editor antes de activar
-- SALES_INTAKE_ENABLED=true en Railway.

CREATE TABLE IF NOT EXISTS sales_intakes (
    conversation_id BIGINT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN (
        'product', 'quantity', 'fulfillment', 'customer', 'confirmation', 'ready_for_isa', 'cancelled'
    )),
    product_request TEXT,
    quantity INTEGER,
    fulfillment TEXT CHECK (fulfillment IN ('shipping', 'pickup')),
    customer_name TEXT,
    customer_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sales_intakes_status_idx
    ON sales_intakes (status, updated_at DESC);
