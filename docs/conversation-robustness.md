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

## Future boundary

WhatsApp Flows may be added if real tests show high drop-off when multiple
free-text fields remain. They are not required for the normal purchase path.
