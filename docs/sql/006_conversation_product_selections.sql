-- Selección de producto por conversación.
-- Ejecutar una sola vez en Supabase > SQL Editor antes de desplegar el flujo
-- conversacional de compra. No crea pedidos ni modifica stock.

CREATE TABLE IF NOT EXISTS conversation_product_selections (
    conversation_id BIGINT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    selected_sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    selected_variant TEXT,
    unit_price NUMERIC(12, 2),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
