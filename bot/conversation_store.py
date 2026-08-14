"""Persistent WhatsApp conversation history stored in Supabase/Postgres.

This module contains no WhatsApp or AI logic. Its only responsibility is to
store messages and return the recent history in the format expected by the
agent. Keeping it separate makes the webhook easier to reason about and test.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import Json
from dotenv import load_dotenv

load_dotenv()


def _connect():
    """Open a database connection without logging the connection URL."""
    database_url = os.getenv("SUPABASE_DB_URL")
    if not database_url:
        raise RuntimeError("Falta SUPABASE_DB_URL en las variables de entorno.")
    return psycopg2.connect(database_url, connect_timeout=10)


def _get_or_create_conversation(cursor, customer_phone: str) -> Tuple[int, str]:
    """Return the conversation ID and its current routing state."""
    cursor.execute(
        """
        INSERT INTO conversations (customer_phone, state, last_message_at)
        VALUES (%s, 'BOT', now())
        ON CONFLICT (customer_phone) DO UPDATE
        SET last_message_at = now()
        RETURNING id, state
        """,
        (customer_phone,),
    )
    conversation_id, state = cursor.fetchone()
    return conversation_id, state


def record_inbound_message(
    customer_phone: str,
    body: str,
    wa_message_id: Optional[str] = None,
    provider_timestamp: Optional[datetime] = None,
) -> Tuple[int, str, bool]:
    """Atomically store one customer message.

    The unique ``wa_message_id`` index is the arbiter.  A Meta retry can no
    longer pass a SELECT and race another worker before the INSERT.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            conversation_id, state = _get_or_create_conversation(cursor, customer_phone)

            cursor.execute(
                """
                INSERT INTO messages (
                    conversation_id, direction, sender, body, wa_message_id,
                    provider_timestamp, received_at
                )
                VALUES (%s, 'in', 'customer', %s, %s, %s, now())
                ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (conversation_id, body, wa_message_id, provider_timestamp),
            )
            inserted = cursor.fetchone()
        connection.commit()
        return conversation_id, state, inserted is None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def enqueue_inbound_message(
    customer_phone: str,
    body: str,
    wa_message_id: str,
    provider_timestamp: Optional[datetime],
    quiet_seconds: float,
    max_burst_seconds: float,
) -> Dict[str, Any]:
    """Persist and schedule one inbound message in one transaction."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            conversation_id, state = _get_or_create_conversation(cursor, customer_phone)
            cursor.execute(
                """
                INSERT INTO messages (
                    conversation_id, direction, sender, body, wa_message_id,
                    provider_timestamp, received_at
                )
                VALUES (%s, 'in', 'customer', %s, %s, %s, now())
                ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL
                DO NOTHING
                RETURNING id, received_at
                """,
                (conversation_id, body, wa_message_id, provider_timestamp),
            )
            inserted = cursor.fetchone()
            if not inserted:
                connection.commit()
                return {
                    "conversation_id": conversation_id,
                    "state": state,
                    "duplicate": True,
                }

            message_id, received_at = inserted
            cursor.execute(
                """
                INSERT INTO conversation_processing (
                    conversation_id, first_pending_at, process_after,
                    latest_message_id, generation, last_processed_message_id,
                    updated_at
                )
                VALUES (
                    %s, %s,
                    LEAST(
                        %s + (%s * interval '1 second'),
                        %s + (%s * interval '1 second')
                    ),
                    %s, 1,
                    -- A conversation can already have history when it first
                    -- enters the durable system (M1 turned on mid-history, or
                    -- a phone that only wrote via the legacy sync path).  Seed
                    -- the watermark so that pre-existing customer messages
                    -- count as already processed; only this message and any
                    -- later ones are pending work. Mirrors
                    -- message_queue.seed_last_processed_message_id.
                    --
                    -- MAX() over zero rows is NULL, not 0: last_processed_message_id
                    -- has a real FK into messages(id), so a literal 0 sentinel
                    -- would violate it the first time a conversation has no
                    -- prior history. NULL is what the column's own
                    -- "ON DELETE SET NULL" design already expects, and
                    -- claim_next_conversation already reads it through
                    -- COALESCE(last_processed_message_id, 0).
                    (SELECT MAX(seed.id)
                     FROM messages seed
                     WHERE seed.conversation_id = %s
                       AND seed.direction = 'in'
                       AND seed.sender = 'customer'
                       AND seed.id < %s),
                    now()
                )
                ON CONFLICT (conversation_id) DO UPDATE SET
                    first_pending_at = COALESCE(
                        conversation_processing.first_pending_at,
                        EXCLUDED.first_pending_at
                    ),
                    process_after = LEAST(
                        COALESCE(
                            conversation_processing.first_pending_at,
                            EXCLUDED.first_pending_at
                        ) + (%s * interval '1 second'),
                        %s + (%s * interval '1 second')
                    ),
                    latest_message_id = EXCLUDED.latest_message_id,
                    generation = conversation_processing.generation + 1,
                    updated_at = now()
                RETURNING generation, process_after
                """,
                (
                    conversation_id,
                    received_at,
                    received_at,
                    max(0.0, max_burst_seconds),
                    received_at,
                    max(0.0, quiet_seconds),
                    message_id,
                    conversation_id,
                    message_id,
                    max(0.0, max_burst_seconds),
                    received_at,
                    max(0.0, quiet_seconds),
                ),
            )
            generation, process_after = cursor.fetchone()
        connection.commit()
        return {
            "conversation_id": conversation_id,
            "state": state,
            "duplicate": False,
            "message_id": message_id,
            "generation": generation,
            "process_after": process_after,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_next_conversation(worker_id: str, lease_seconds: float) -> Optional[Dict[str, Any]]:
    """Lease one ready conversation with ``SKIP LOCKED``.

    Multiple Railway workers can call this concurrently; Postgres returns a
    given conversation to at most one of them until the lease expires.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH candidate AS (
                    SELECT conversation_id
                    FROM conversation_processing
                    WHERE process_after IS NOT NULL
                      AND process_after <= now()
                      AND (lease_until IS NULL OR lease_until <= now())
                    ORDER BY process_after, conversation_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE conversation_processing AS processing
                SET lease_owner = %s,
                    lease_until = now() + (%s * interval '1 second'),
                    updated_at = now()
                FROM candidate
                WHERE processing.conversation_id = candidate.conversation_id
                RETURNING processing.conversation_id,
                          processing.generation,
                          processing.latest_message_id,
                          processing.last_processed_message_id,
                          processing.lease_until
                """,
                (worker_id, max(1.0, lease_seconds)),
            )
            leased = cursor.fetchone()
            if not leased:
                connection.commit()
                return None

            conversation_id, generation, latest_id, last_processed_id, lease_until = leased
            cursor.execute(
                "SELECT customer_phone, state FROM conversations WHERE id = %s",
                (conversation_id,),
            )
            customer_phone, state = cursor.fetchone()
            cursor.execute(
                """
                SELECT id, body, wa_message_id, provider_timestamp, received_at
                FROM messages
                WHERE conversation_id = %s
                  AND direction = 'in'
                  AND sender = 'customer'
                  AND id > COALESCE(%s, 0)
                  AND id <= %s
                ORDER BY COALESCE(provider_timestamp, received_at), received_at, id
                """,
                (conversation_id, last_processed_id, latest_id),
            )
            message_rows = cursor.fetchall()
            if not message_rows:
                cursor.execute(
                    """
                    UPDATE conversation_processing
                    SET lease_owner = NULL, lease_until = NULL,
                        process_after = NULL, first_pending_at = NULL,
                        updated_at = now()
                    WHERE conversation_id = %s AND lease_owner = %s
                    """,
                    (conversation_id, worker_id),
                )
                connection.commit()
                return None

            first_message_id = min(row[0] for row in message_rows)
            cursor.execute(
                """
                SELECT direction, sender, body
                FROM messages
                WHERE conversation_id = %s AND id < %s
                ORDER BY id DESC
                LIMIT 12
                """,
                (conversation_id, first_message_id),
            )
            history_rows = cursor.fetchall()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    history: List[Dict[str, Any]] = []
    for direction, sender, text in reversed(history_rows):
        role = "user" if direction == "in" and sender == "customer" else "assistant"
        history.append({"role": role, "content": text})
    return {
        "conversation_id": conversation_id,
        "customer_phone": customer_phone,
        "state": state,
        "generation": generation,
        "latest_message_id": latest_id,
        "last_processed_message_id": last_processed_id,
        "lease_owner": worker_id,
        "lease_until": lease_until,
        "history": history,
        "messages": [
            {
                "id": row[0],
                "body": row[1],
                "wa_message_id": row[2],
                "provider_timestamp": row[3],
                "received_at": row[4],
            }
            for row in message_rows
        ],
    }


def processing_claim_is_current(conversation_id: int, generation: int, worker_id: str) -> bool:
    """Check both ownership and the generation watermark before delivery."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM conversation_processing
                WHERE conversation_id = %s
                  AND generation = %s
                  AND lease_owner = %s
                  AND lease_until > now()
                """,
                (conversation_id, generation, worker_id),
            )
            return cursor.fetchone() is not None
    finally:
        connection.close()


def renew_processing_claim(
    conversation_id: int, generation: int, worker_id: str, lease_seconds: float
) -> bool:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE conversation_processing
                SET lease_until = now() + (%s * interval '1 second'), updated_at = now()
                WHERE conversation_id = %s
                  AND generation = %s
                  AND lease_owner = %s
                  AND lease_until > now()
                RETURNING 1
                """,
                (max(1.0, lease_seconds), conversation_id, generation, worker_id),
            )
            renewed = cursor.fetchone() is not None
        connection.commit()
        return renewed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_processing_claim(
    conversation_id: int,
    generation: int,
    worker_id: str,
    last_message_id: int,
    delivered: bool,
) -> bool:
    """Advance processed/delivered watermarks only for the current claim."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE conversation_processing
                SET last_processed_message_id = %s,
                    last_delivered_message_id = CASE
                        WHEN %s THEN %s
                        ELSE last_delivered_message_id
                    END,
                    first_pending_at = NULL,
                    process_after = NULL,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = now()
                WHERE conversation_id = %s
                  AND generation = %s
                  AND lease_owner = %s
                RETURNING 1
                """,
                (
                    last_message_id,
                    delivered,
                    last_message_id,
                    conversation_id,
                    generation,
                    worker_id,
                ),
            )
            finished = cursor.fetchone() is not None
        connection.commit()
        return finished
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_processing_claim(conversation_id: int, worker_id: str) -> None:
    """Release only a lease still owned by this worker; keep pending work."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE conversation_processing
                SET lease_owner = NULL, lease_until = NULL, updated_at = now()
                WHERE conversation_id = %s AND lease_owner = %s
                """,
                (conversation_id, worker_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_bot_message(conversation_id: int, body: str) -> None:
    """Store a reply written by the bot after it was successfully sent."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, direction, sender, body)
                VALUES (%s, 'out', 'bot', %s)
                """,
                (conversation_id, body),
            )
            cursor.execute(
                "UPDATE conversations SET last_message_at = now() WHERE id = %s",
                (conversation_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_isa_feedback(
    isa_phone: str,
    body: str,
    wa_message_id: Optional[str] = None,
) -> bool:
    """Store internal feedback from Isa without affecting a customer conversation.

    Returns False when Meta retries an already-recorded WhatsApp message.
    Feedback is deliberately stored as a message for now: it is auditable, but
    it does not alter prompts, products, orders, or customer replies by itself.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            conversation_id, _state = _get_or_create_conversation(cursor, isa_phone)

            cursor.execute(
                "UPDATE conversations SET state = 'ISA', last_message_at = now() WHERE id = %s",
                (conversation_id,),
            )
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, direction, sender, body, wa_message_id)
                VALUES (%s, 'in', 'isa', %s, %s)
                ON CONFLICT (wa_message_id) WHERE wa_message_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """,
                (conversation_id, "FEEDBACK: " + body, wa_message_id),
            )
            inserted = cursor.fetchone()
        connection.commit()
        return inserted is not None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_history(customer_phone: str, limit: int = 12) -> List[Dict[str, Any]]:
    """Return the last messages oldest-first in the DeepSeek chat format."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT direction, sender, body
                FROM messages
                WHERE conversation_id = (
                    SELECT id FROM conversations WHERE customer_phone = %s
                )
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (customer_phone, limit),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    history: List[Dict[str, Any]] = []
    for direction, sender, body in reversed(rows):
        role = "user" if direction == "in" and sender == "customer" else "assistant"
        history.append({"role": role, "content": body})
    return history


def is_latest_customer_message(conversation_id: int, wa_message_id: str) -> bool:
    """Return whether this event is still the newest customer message.

    This small database-backed check makes the webhook debounce work across
    Railway instances: only the last message in a burst is allowed to answer.
    It is deliberately not a replacement for a future durable worker queue.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT wa_message_id
                FROM messages
                WHERE conversation_id = %s
                  AND direction = 'in'
                  AND sender = 'customer'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    return bool(row and row[0] == wa_message_id)


def load_open_customer_turn(customer_phone: str, limit: int = 12) -> str:
    """Join the customer's messages since Fred's last reply into one turn."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH conversation AS (
                    SELECT id FROM conversations WHERE customer_phone = %s
                ), last_bot_reply AS (
                    SELECT COALESCE(MAX(id), 0) AS id
                    FROM messages
                    WHERE conversation_id = (SELECT id FROM conversation)
                      AND direction = 'out'
                )
                SELECT body
                FROM messages
                WHERE conversation_id = (SELECT id FROM conversation)
                  AND direction = 'in'
                  AND sender = 'customer'
                  AND id > (SELECT id FROM last_bot_reply)
                ORDER BY id ASC
                LIMIT %s
                """,
                (customer_phone, limit),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return "\n".join(row[0].strip() for row in rows if row[0] and row[0].strip())


def get_active_sales_intake(conversation_id: int) -> Optional[Dict[str, Any]]:
    """Return the active sales-intake form for one customer conversation."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, product_request, selected_sku, selected_variant, unit_price, quantity,
                       fulfillment, customer_name, customer_email
                FROM sales_intakes
                WHERE conversation_id = %s
                  AND status NOT IN ('ready_for_isa', 'cancelled')
                """,
                (conversation_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()

    if not row:
        return None
    return {
        "status": row[0],
        "product_request": row[1],
        "selected_sku": row[2],
        "selected_variant": row[3],
        "unit_price": row[4],
        "quantity": row[5],
        "fulfillment": row[6],
        "customer_name": row[7],
        "customer_email": row[8],
    }


def save_product_selection(conversation_id: int, candidate: Dict[str, Any]) -> None:
    """Remember the last concrete, live-verified product a customer chose.

    This is deliberately separate from ``sales_intakes``: showing interest in
    a product is not yet a purchase. It lets a natural next message such as
    “quiero dos, envío” keep the product without forcing the customer to type
    the model name again.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO conversation_product_selections (
                    conversation_id, selected_sku, product_name, selected_variant, unit_price
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                SET selected_sku = EXCLUDED.selected_sku,
                    product_name = EXCLUDED.product_name,
                    selected_variant = EXCLUDED.selected_variant,
                    unit_price = EXCLUDED.unit_price,
                    updated_at = now()
                """,
                (
                    conversation_id,
                    candidate.get("sku"),
                    candidate.get("product_name"),
                    candidate.get("variant") or None,
                    candidate.get("unit_price"),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_product_selection(conversation_id: int) -> Optional[Dict[str, Any]]:
    """Return the last product selection; stock is revalidated by the caller."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT selected_sku, product_name, selected_variant, unit_price
                FROM conversation_product_selections
                WHERE conversation_id = %s
                """,
                (conversation_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return {
        "sku": row[0],
        "product_name": row[1],
        "variant": row[2] or "",
        "unit_price": row[3],
    }


def clear_product_selection(conversation_id: int) -> None:
    """Forget a prior product when the customer starts a different request."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM conversation_product_selections WHERE conversation_id = %s",
                (conversation_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


_FRED_CORE_FIELDS = (
    "mode", "active_product_id", "active_product_name", "active_sku",
    "active_variant", "unit_price", "quantity", "delivery_method",
    "customer_name", "customer_email", "postal_code", "checkout_step",
    "order_number", "pending_intent",
)


def get_fred_core_state(conversation_id: int) -> Dict[str, Any]:
    """The single source of truth for where this conversation is: an
    explicit mode plus structured fields, never reconstructed by reading
    Fred's own prior message text. A conversation Fred Core hasn't touched
    yet defaults to CHAT with everything else empty."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mode, active_product_id, active_product_name, active_sku,
                       active_variant, unit_price, quantity, delivery_method,
                       customer_name, customer_email, postal_code, checkout_step,
                       order_number, pending_intent
                FROM fred_core_state
                WHERE conversation_id = %s
                """,
                (conversation_id,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        state = {field: None for field in _FRED_CORE_FIELDS}
        state["mode"] = "CHAT"
        return state
    return dict(zip(_FRED_CORE_FIELDS, row))


def save_fred_core_state(conversation_id: int, **fields: Any) -> None:
    """Upsert only the given fields; every other field keeps its current
    value. This is the only function that persists Fred Core's mode/active
    product/checkout data -- no other code path should write a second,
    competing notion of conversation state."""
    unknown = set(fields) - set(_FRED_CORE_FIELDS)
    if unknown:
        raise ValueError("Campos desconocidos para fred_core_state: {}".format(sorted(unknown)))
    if not fields:
        return
    columns = list(fields.keys())
    values = [fields[column] for column in columns]
    insert_columns = ["conversation_id"] + columns
    update_assignments = ", ".join("{0} = EXCLUDED.{0}".format(column) for column in columns)
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO fred_core_state ({columns})
                VALUES ({placeholders})
                ON CONFLICT (conversation_id) DO UPDATE SET
                    {updates},
                    updated_at = now()
                """.format(
                    columns=", ".join(insert_columns),
                    placeholders=", ".join(["%s"] * len(insert_columns)),
                    updates=update_assignments,
                ),
                [conversation_id] + values,
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reset_fred_core_checkout(conversation_id: int) -> None:
    """Return to CHAT and clear checkout-only fields. The active product and
    any known order_number are left untouched -- still relevant context for
    the next chat turn."""
    save_fred_core_state(
        conversation_id,
        mode="CHAT", quantity=None, delivery_method=None,
        customer_name=None, customer_email=None, postal_code=None,
        checkout_step=None, pending_intent=None,
    )


def start_sales_intake(
    conversation_id: int,
    product_request: str = "",
    selected_sku: str = "",
    selected_variant: str = "",
    unit_price: Optional[str] = None,
    quantity: Optional[int] = None,
) -> None:
    """Start or reset the pre-approval sales form. It never creates an order."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sales_intakes (
                    conversation_id, status, product_request, selected_sku, selected_variant, unit_price, quantity
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (conversation_id) DO UPDATE
                SET status = EXCLUDED.status,
                    product_request = EXCLUDED.product_request,
                    selected_sku = EXCLUDED.selected_sku,
                    selected_variant = EXCLUDED.selected_variant,
                    unit_price = EXCLUDED.unit_price,
                    quantity = EXCLUDED.quantity,
                    fulfillment = NULL,
                    customer_name = NULL,
                    customer_email = NULL,
                    updated_at = now()
                """,
                (
                    conversation_id,
                    "fulfillment" if quantity else "quantity" if product_request else "product",
                    product_request or None,
                    selected_sku or None,
                    selected_variant or None,
                    unit_price,
                    quantity,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_sales_intake_product(conversation_id: int, product_request: str) -> None:
    _update_sales_intake(
        conversation_id,
        "quantity",
        product_request=product_request,
        selected_sku=None,
        selected_variant=None,
        unit_price=None,
    )


def set_sales_intake_quantity(conversation_id: int, quantity: int) -> None:
    _update_sales_intake(conversation_id, "fulfillment", quantity=quantity)


def set_sales_intake_fulfillment(conversation_id: int, fulfillment: str) -> None:
    if fulfillment not in ("shipping", "pickup"):
        raise ValueError("Invalid fulfillment")
    _update_sales_intake(conversation_id, "customer", fulfillment=fulfillment)


def set_sales_intake_customer(
    conversation_id: int,
    customer_name: str,
    customer_email: str,
) -> None:
    _update_sales_intake(
        conversation_id,
        "confirmation",
        customer_name=customer_name,
        customer_email=customer_email,
    )


def update_sales_intake_fields(
    conversation_id: int,
    status: str,
    **values: Any,
) -> None:
    """Apply explicit corrections without discarding the rest of a sale draft."""
    _update_sales_intake(conversation_id, status, **values)


def mark_sales_intake_ready(conversation_id: int) -> None:
    _update_sales_intake(conversation_id, "ready_for_isa")


def cancel_sales_intake(conversation_id: int) -> None:
    _update_sales_intake(conversation_id, "cancelled")


def _update_sales_intake(conversation_id: int, status: str, **values: Any) -> None:
    """Persist one explicit sales-form transition."""
    allowed_statuses = {
        "product", "quantity", "fulfillment", "customer", "confirmation", "ready_for_isa", "cancelled"
    }
    if status not in allowed_statuses:
        raise ValueError("Invalid sales intake status")

    allowed_fields = {
        "product_request", "selected_sku", "selected_variant", "unit_price", "quantity", "fulfillment",
        "customer_name", "customer_email"
    }
    unknown = set(values) - allowed_fields
    if unknown:
        raise ValueError("Invalid sales intake fields")

    assignments = ["status = %s", "updated_at = now()"]
    parameters: List[Any] = [status]
    for field, value in values.items():
        assignments.append("{} = %s".format(field))
        parameters.append(value)
    parameters.append(conversation_id)

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE sales_intakes SET {} WHERE conversation_id = %s".format(
                    ", ".join(assignments)
                ),
                parameters,
            )
            if cursor.rowcount != 1:
                raise RuntimeError("No existe una ficha de venta activa.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def pending_action_count() -> int:
    """Return the global number of actions waiting for Isa."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pending_actions WHERE status = 'pending'")
            return int(cursor.fetchone()[0])
    finally:
        connection.close()


def pending_reminder_snapshot() -> Dict[str, Any]:
    """Return queue size and the age anchor for non-intrusive reminders."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*), min(created_at)
                FROM pending_actions
                WHERE status = 'pending'
                """
            )
            count, oldest = cursor.fetchone()
    finally:
        connection.close()
    return {"count": int(count), "oldest_created_at": oldest}


def isa_reminders_snoozed(isa_phone: str) -> bool:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT snoozed_until > now()
                FROM isa_reminder_preferences
                WHERE isa_phone = %s
                """,
                (isa_phone,),
            )
            row = cursor.fetchone()
            return bool(row and row[0])
    finally:
        connection.close()


def snooze_isa_reminders(isa_phone: str, until: datetime) -> None:
    """Pause automatic reminders until the requested instant."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO isa_reminder_preferences (isa_phone, snoozed_until, requested_reminder_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (isa_phone) DO UPDATE
                SET snoozed_until = EXCLUDED.snoozed_until,
                    requested_reminder_at = EXCLUDED.requested_reminder_at,
                    updated_at = now()
                """,
                (isa_phone, until, until),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear_isa_reminder_snooze(isa_phone: str) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO isa_reminder_preferences (isa_phone, snoozed_until, requested_reminder_at)
                VALUES (%s, NULL, NULL)
                ON CONFLICT (isa_phone) DO UPDATE
                SET snoozed_until = NULL, requested_reminder_at = NULL, updated_at = now()
                """,
                (isa_phone,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_requested_isa_reminder(isa_phone: str) -> bool:
    """Claim one explicit 'remind me later' request, exactly once."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE isa_reminder_preferences
                SET requested_reminder_at = NULL, snoozed_until = NULL, updated_at = now()
                WHERE isa_phone = %s
                  AND requested_reminder_at IS NOT NULL
                  AND requested_reminder_at <= now()
                RETURNING isa_phone
                """,
                (isa_phone,),
            )
            claimed = cursor.fetchone() is not None
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_daily_isa_reminder(isa_phone: str, reminder_kind: str, local_date) -> bool:
    """Atomically reserve one automatic reminder of each kind per local day."""
    if reminder_kind not in ("gentle", "follow_up"):
        raise ValueError("Invalid reminder kind")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO isa_reminder_events (isa_phone, reminder_kind, local_date)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING isa_phone
                """,
                (isa_phone, reminder_kind, local_date),
            )
            claimed = cursor.fetchone() is not None
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_daily_isa_reminder(isa_phone: str, reminder_kind: str, local_date) -> None:
    """Allow a retry when Meta did not accept a reserved reminder."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM isa_reminder_events
                WHERE isa_phone = %s AND reminder_kind = %s AND local_date = %s
                """,
                (isa_phone, reminder_kind, local_date),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_pending_action(
    conversation_id: int,
    action_type: str,
    summary: str,
    payload: Optional[Dict[str, Any]] = None,
) -> int:
    """Create an approval/handoff draft. It never creates a Tiendanube order."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pending_actions (conversation_id, action_type, summary, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (conversation_id, action_type, summary, Json(payload or {})),
            )
            action_id = int(cursor.fetchone()[0])
        connection.commit()
        return action_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_conversation_state(conversation_id: int, state: str) -> None:
    """Route a conversation to the bot or Isa."""
    if state not in ("BOT", "ESCALATED", "ISA"):
        raise ValueError("Invalid conversation state")

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conversations SET state = %s, last_message_at = now() WHERE id = %s",
                (state, conversation_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_pending_actions(limit: int = 10) -> List[Dict[str, Any]]:
    """Return pending actions oldest-first, including the customer phone."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    pending_actions.id,
                    pending_actions.action_type,
                    pending_actions.summary,
                    pending_actions.payload,
                    pending_actions.created_at,
                    conversations.customer_phone,
                    pending_actions.conversation_id
                FROM pending_actions
                JOIN conversations ON conversations.id = pending_actions.conversation_id
                WHERE pending_actions.status = 'pending'
                ORDER BY pending_actions.created_at ASC, pending_actions.id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    return [
        {
            "id": int(row[0]),
            "action_type": row[1],
            "summary": row[2],
            "payload": row[3] or {},
            "created_at": row[4],
            "customer_phone": row[5],
            "conversation_id": int(row[6]),
        }
        for row in rows
    ]


def resolve_pending_action(action_id: int, status: str) -> Optional[Dict[str, Any]]:
    """Approve or reject a draft; order creation remains a later explicit step."""
    if status not in ("approved", "rejected"):
        raise ValueError("Invalid pending action status")

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET status = %s, resolved_at = now()
                WHERE id = %s AND status = 'pending'
                RETURNING conversation_id, action_type, summary, payload
                """,
                (status, action_id),
            )
            row = cursor.fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    if not row:
        return None
    return {
        "conversation_id": int(row[0]),
        "action_type": row[1],
        "summary": row[2],
        "payload": row[3] or {},
        "status": status,
    }


def wait_for_isa_response(action_id: int) -> bool:
    """Mark an Isa-assisted case while Fred waits for her written answer.

    Only one pending case may await her next free-text reply at a time: if
    she taps "Responder a Fred" on a different case before typing an answer
    to an earlier one, that earlier flag is cleared first. Otherwise her
    next message would be delivered to whichever case happens to be oldest,
    not the one she just chose -- a real risk of sending her answer to the
    wrong customer.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = payload - 'awaiting_isa_response'
                WHERE payload ? 'awaiting_isa_response'
                  AND id != %s
                """,
                (action_id,),
            )
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = jsonb_set(payload, '{awaiting_isa_response}', 'true'::jsonb, true)
                WHERE id = %s
                  AND status = 'pending'
                  AND action_type IN ('bot_fallback', 'human_handoff', 'special_sale_request')
                """,
                (action_id,),
            )
            updated = cursor.rowcount == 1
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_isa_awaiting(action_id: int, kind: str) -> bool:
    """Record WHAT Isa's next free-text message is answering for one case.

    Same exclusivity rule as wait_for_isa_response: only one case may await
    her typing at a time, so an answer can never be delivered against the
    wrong customer. Unlike that helper this one also covers purchase_review
    (rejection reason, question for the customer) and remembers the kind, so
    the same message can mean "motivo del rechazo" or "pregunta para la
    clienta" without guessing from its text.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = (payload - 'awaiting_isa_response') - 'awaiting_isa_kind'
                WHERE (payload ? 'awaiting_isa_response' OR payload ? 'awaiting_isa_kind')
                  AND id != %s
                """,
                (action_id,),
            )
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = jsonb_set(payload, '{awaiting_isa_kind}', to_jsonb(%s::text), true)
                WHERE id = %s AND status = 'pending'
                """,
                (kind, action_id),
            )
            updated = cursor.rowcount == 1
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear_isa_awaiting(action_id: int) -> None:
    """Forget a pending question once it was answered or abandoned."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = (payload - 'awaiting_isa_response') - 'awaiting_isa_kind'
                WHERE id = %s
                """,
                (action_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_pending_action_checkout(action_id: int, checkout: Dict[str, Any]) -> bool:
    """Persist an approved checkout before delivering its link over WhatsApp.

    This makes an approval retry idempotent: Fred reuses the same checkout URL
    instead of accidentally creating a second cart/order.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = jsonb_set(payload, '{checkout}', %s::jsonb, true)
                WHERE id = %s AND status = 'pending'
                """,
                (Json(checkout).dumps(checkout), action_id),
            )
            saved = cursor.rowcount == 1
        connection.commit()
        return saved
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def start_isa_sale_session(isa_phone: str) -> None:
    """Start a guided internal sale draft for Isa; never creates an order."""
    _update_isa_sale_session(isa_phone, "choose_type", sale_type=None, details=None)


def set_isa_sale_session_type(isa_phone: str, sale_type: str) -> None:
    """Persist one of the approved manual-sale categories."""
    if sale_type not in ("normal", "encargo", "venta_mayorista", "otro"):
        raise ValueError("Invalid internal sale type")
    _update_isa_sale_session(isa_phone, "collect_details", sale_type=sale_type)


def get_isa_sale_session(isa_phone: str) -> Optional[Dict[str, Any]]:
    """Return Isa's current guided draft, if any."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, sale_type, details
                FROM isa_sale_sessions
                WHERE isa_phone = %s
                """,
                (isa_phone,),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return {"status": row[0], "sale_type": row[1], "details": row[2]}


def add_isa_sale_session_details(isa_phone: str, details: str) -> None:
    """Store the free-form sale details supplied by Isa for later review."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE isa_sale_sessions
                SET details = %s, status = 'review', updated_at = now()
                WHERE isa_phone = %s AND status = 'collect_details'
                """,
                (details.strip(), isa_phone),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("No existe un borrador interno esperando datos.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear_isa_sale_session(isa_phone: str) -> None:
    """Discard an internal draft without affecting customer conversations."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM isa_sale_sessions WHERE isa_phone = %s", (isa_phone,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _update_isa_sale_session(isa_phone: str, status: str, **values: Any) -> None:
    """Upsert the short-lived internal sale workflow state."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO isa_sale_sessions (isa_phone, status, sale_type, details)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (isa_phone) DO UPDATE
                SET status = EXCLUDED.status,
                    sale_type = EXCLUDED.sale_type,
                    details = EXCLUDED.details,
                    updated_at = now()
                """,
                (isa_phone, status, values.get("sale_type"), values.get("details")),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
