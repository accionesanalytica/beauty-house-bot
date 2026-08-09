"""Persistent WhatsApp conversation history stored in Supabase/Postgres.

This module contains no WhatsApp or AI logic. Its only responsibility is to
store messages and return the recent history in the format expected by the
agent. Keeping it separate makes the webhook easier to reason about and test.
"""

import os
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
                SELECT status, product_request, quantity, fulfillment, customer_name, customer_email
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
        "quantity": row[2],
        "fulfillment": row[3],
        "customer_name": row[4],
        "customer_email": row[5],
    }


def start_sales_intake(conversation_id: int) -> None:
    """Start or reset the pre-approval sales form. It never creates an order."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sales_intakes (conversation_id, status)
                VALUES (%s, 'product')
                ON CONFLICT (conversation_id) DO UPDATE
                SET status = 'product',
                    product_request = NULL,
                    quantity = NULL,
                    fulfillment = NULL,
                    customer_name = NULL,
                    customer_email = NULL,
                    updated_at = now()
                """,
                (conversation_id,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_sales_intake_product(conversation_id: int, product_request: str) -> None:
    _update_sales_intake(conversation_id, "quantity", product_request=product_request)


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
        "product_request", "quantity", "fulfillment", "customer_name", "customer_email"
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
