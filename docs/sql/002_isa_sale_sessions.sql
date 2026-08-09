-- Borradores internos guiados para Isa.
-- No crea pedidos, links, pagos ni movimientos de stock.
-- Ejecutar una vez en Supabase > SQL Editor antes de usar el menú interno.

CREATE TABLE IF NOT EXISTS isa_sale_sessions (
    isa_phone TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('choose_type', 'collect_details', 'review')),
    sale_type TEXT CHECK (sale_type IN ('normal', 'encargo', 'venta_mayorista', 'otro')),
    details TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
