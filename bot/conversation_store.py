"""Persistent WhatsApp conversation history stored in Supabase/Postgres.

This module contains no WhatsApp or AI logic. Its only responsibility is to
store messages and return the recent history in the format expected by the
agent. Keeping it separate makes the webhook easier to reason about and test.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
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
