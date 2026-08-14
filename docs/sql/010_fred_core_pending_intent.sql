-- Fred Core: recordar QUÉ acaba de preguntar Fred cuando esa pregunta, al ser
-- respondida, ejecuta o modifica algo.
--
-- Puramente aditivo: sólo agrega una columna nullable a fred_core_state. No
-- modifica conversations, sales_intakes, pending_actions ni ninguna otra
-- tabla. No crea pedidos ni modifica stock. Las conversaciones existentes
-- quedan con pending_intent NULL, que es exactamente "no hay nada pendiente".
--
-- Motivo: un "sí" / "dale" / "confirmo" sólo puede resolverse correctamente
-- si el estado sabe a qué pregunta está respondiendo. Sin esto, una
-- confirmación natural volvía a caer en descubrimiento de producto y
-- respondía sobre un producto equivocado.

ALTER TABLE fred_core_state
    ADD COLUMN IF NOT EXISTS pending_intent TEXT;
