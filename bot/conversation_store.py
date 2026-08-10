"""Persistent WhatsApp conversation history stored in Supabase/Postgres.

This module contains no WhatsApp or AI logic. Its only responsibility is to
store messages and return the recent history in the format expected by the
agent. Keeping it separate makes the webhook easier to reason about and test.
"""

import os
from datetime import datetime
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
) -> Tuple[int, str, bool]:
    """Store one customer message and return (conversation_id, state, duplicate)."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            conversation_id, state = _get_or_create_conversation(cursor, customer_phone)

            if wa_message_id:
                cursor.execute(
                    "SELECT 1 FROM messages WHERE wa_message_id = %s LIMIT 1",
                    (wa_message_id,),
                )
                if cursor.fetchone():
                    connection.commit()
                    return conversation_id, state, True

            cursor.execute(
                """
                INSERT INTO messages (conversation_id, direction, sender, body, wa_message_id)
                VALUES (%s, 'in', 'customer', %s, %s)
                """,
                (conversation_id, body, wa_message_id),
            )
        connection.commit()
        return conversation_id, state, False
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

            if wa_message_id:
                cursor.execute(
                    "SELECT 1 FROM messages WHERE wa_message_id = %s LIMIT 1",
                    (wa_message_id,),
                )
                if cursor.fetchone():
                    connection.commit()
                    return False

            cursor.execute(
                "UPDATE conversations SET state = 'ISA', last_message_at = now() WHERE id = %s",
                (conversation_id,),
            )
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, direction, sender, body, wa_message_id)
                VALUES (%s, 'in', 'isa', %s, %s)
                """,
                (conversation_id, "FEEDBACK: " + body, wa_message_id),
            )
        connection.commit()
        return True
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
                    conversations.customer_phone
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
    """Mark a consultation pending while Fred waits for Isa's written answer."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE pending_actions
                SET payload = jsonb_set(payload, '{awaiting_isa_response}', 'true'::jsonb, true)
                WHERE id = %s AND status = 'pending' AND action_type = 'bot_fallback'
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
