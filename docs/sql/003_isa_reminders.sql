-- Recordatorios persistentes para pendientes de Isa.
-- Ejecutar una vez en Supabase > SQL Editor.

CREATE TABLE IF NOT EXISTS isa_reminder_preferences (
    isa_phone TEXT PRIMARY KEY,
    snoozed_until TIMESTAMPTZ,
    requested_reminder_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS isa_reminder_events (
    isa_phone TEXT NOT NULL,
    reminder_kind TEXT NOT NULL CHECK (reminder_kind IN ('gentle', 'follow_up')),
    local_date DATE NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (isa_phone, reminder_kind, local_date)
);
