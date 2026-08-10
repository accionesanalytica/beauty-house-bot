# Conversation Robustness

Fred treats a short burst of WhatsApp bubbles as one human turn. The webhook
stores every message first, waits `CONVERSATION_DEBOUNCE_SECONDS` (default 1.5),
and only the newest stored event in that conversation replies. It then joins
the customer messages since Fred's last reply before deciding.

This is database-backed, so a later Railway replica does not rely on an
in-memory timer to know which bubble is latest. It is intentionally bounded at
four seconds. It is not a replacement for a future durable worker queue: a
long-running tool can still receive a newer message after it begins.

## Adaptive purchase intake

Before a sale exists, Fred remembers the last **named, live-verified** product
selected in a conversation (SKU, variant and reference price). Interest is not
an order: this small selection state is separate from the sales form.

That means this natural sequence is valid:

1. Clienta: “Quiero las Isabel I chocolate.”
2. Fred verifies the SKU and remembers that choice.
3. Clienta: “Quiero dos, envío. Nombre: Laura Pérez. Email: laura@…”
4. Fred rechecks Tiendanube stock, opens the real intake with Isabel I, and
   preserves every detail in the same message.

The model cannot open a blank sales form. Only code can open one, and only
after a concrete SKU has been verified against current Tiendanube stock. If
the product is ambiguous, Fred asks for a model or link instead of asking for
generic customer fields.

Every message during an active purchase can update explicit fields. Latest
explicit value wins for quantity, fulfillment, name and email. Fred asks only
for missing information:

1. quantity
2. fulfillment (`Envío` / `Retiro` buttons when available)
3. name and email
4. customer confirmation
5. Isa approval

Address, locality and postal code are intentionally collected by Tiendanube's
checkout, not duplicated in WhatsApp. The customer and Isa both see the full
email in the sale summary so they can catch a typo before approval.

Database prerequisite: run `docs/sql/006_conversation_product_selections.sql`
once in Supabase before enabling this behavior in production.

## Future boundary

WhatsApp Flows may be added if real tests show high drop-off when multiple
free-text fields remain. They are not required for the normal purchase path.
