"""Operational storage for payment events and the private Fred dashboard.

These records are intentionally separate from the chat state.  A Tiendanube
webhook may be retried or arrive out of order, so it needs its own idempotency
ledger before any WhatsApp notification is sent.
"""

import os
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json


def _connect():
    database_url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not database_url:
        raise RuntimeError("Falta SUPABASE_DB_URL en Railway.")
    return psycopg2.connect(database_url, connect_timeout=10)


def _ensure_storage(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS tiendanube_webhook_events (
            event_key TEXT PRIMARY KEY,
            store_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            order_id TEXT,
            payload JSONB NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('received', 'processed', 'ignored', 'failed')),
            error_type TEXT,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS tiendanube_webhook_events_received_idx
        ON tiendanube_webhook_events (received_at DESC)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS fred_daily_reports (
            report_day DATE PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('sending', 'sent')),
            sent_at TIMESTAMPTZ
        )
        """
    )


def claim_tiendanube_event(
    event_key: str,
    store_id: str,
    event_name: str,
    order_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Save an inbound event exactly once; False means a retry/duplicate."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                INSERT INTO tiendanube_webhook_events (
                    event_key, store_id, event_name, order_id, payload, status
                ) VALUES (%s, %s, %s, %s, %s, 'received')
                ON CONFLICT (event_key) DO NOTHING
                """,
                (event_key, store_id, event_name, order_id or None, Json(payload)),
            )
            claimed = cursor.rowcount == 1
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_tiendanube_event(event_key: str, status: str, error_type: str = "") -> None:
    if status not in ("processed", "ignored", "failed"):
        raise ValueError("Estado de evento inválido")
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                UPDATE tiendanube_webhook_events
                SET status = %s, error_type = %s, processed_at = now()
                WHERE event_key = %s
                """,
                (status, error_type or None, event_key),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fred_checkout_for_order(order_id: str) -> Optional[Dict[str, Any]]:
    """Find a Fred-created checkout without touching unrelated store orders."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pending_actions.id, pending_actions.conversation_id,
                       pending_actions.payload, conversations.customer_phone
                FROM pending_actions
                JOIN conversations ON conversations.id = pending_actions.conversation_id
                WHERE pending_actions.action_type = 'purchase_review'
                  AND pending_actions.status = 'approved'
                  AND pending_actions.payload -> 'checkout' ->> 'id' = %s
                ORDER BY pending_actions.resolved_at DESC NULLS LAST
                LIMIT 1
                """,
                (str(order_id),),
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    if not row:
        return None
    return {
        "action_id": int(row[0]),
        "conversation_id": int(row[1]),
        "payload": row[2] or {},
        "customer_phone": row[3],
    }


def dashboard_snapshot(limit: int = 40) -> Dict[str, Any]:
    """Read-only operational overview for the private owner dashboard."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM conversations
                     WHERE last_message_at >= now() - interval '24 hours'),
                    (SELECT count(*) FROM messages
                     WHERE direction = 'in' AND sender = 'customer'
                       AND created_at >= now() - interval '24 hours'),
                    (SELECT count(*) FROM messages
                     WHERE direction = 'out' AND sender = 'bot'
                       AND created_at >= now() - interval '24 hours'),
                    (SELECT count(*) FROM pending_actions WHERE status = 'pending'),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'purchase_review' AND status = 'approved'
                       AND resolved_at >= now() - interval '24 hours'),
                    (SELECT count(*) FROM tiendanube_webhook_events
                     WHERE event_name = 'order/paid' AND status = 'processed'
                       AND received_at >= now() - interval '24 hours')
                """
            )
            counts = cursor.fetchone()
            cursor.execute(
                """
                SELECT c.id, c.customer_phone, c.state, c.last_message_at,
                       COALESCE(last_message.body, '')
                FROM conversations c
                LEFT JOIN LATERAL (
                    SELECT body FROM messages
                    WHERE conversation_id = c.id
                    ORDER BY created_at DESC, id DESC LIMIT 1
                ) last_message ON true
                ORDER BY c.last_message_at DESC, c.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            conversations = cursor.fetchall()
            cursor.execute(
                """
                SELECT action_type, count(*)
                FROM pending_actions
                WHERE status = 'pending'
                GROUP BY action_type
                """
            )
            pending_by_type = {row[0]: int(row[1]) for row in cursor.fetchall()}
    finally:
        connection.close()

    return {
        "last_24h": {
            "active_conversations": int(counts[0]),
            "customer_messages": int(counts[1]),
            "fred_messages": int(counts[2]),
            "pending_actions": int(counts[3]),
            "approved_checkouts": int(counts[4]),
            "fred_paid_orders": int(counts[5]),
        },
        "pending_by_type": pending_by_type,
        "conversations": [
            {
                "id": int(row[0]),
                "customer_phone": row[1],
                "state": row[2],
                "last_message_at": row[3],
                "last_message": row[4],
            }
            for row in conversations
        ],
    }


def dashboard_conversation(conversation_id: int) -> Optional[Dict[str, Any]]:
    """Return one full chat for the owner-only dashboard."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, customer_phone, state, last_message_at FROM conversations WHERE id = %s",
                (conversation_id,),
            )
            conversation = cursor.fetchone()
            if not conversation:
                return None
            cursor.execute(
                """
                SELECT direction, sender, body, created_at
                FROM messages WHERE conversation_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (conversation_id,),
            )
            messages = cursor.fetchall()
    finally:
        connection.close()
    return {
        "id": int(conversation[0]),
        "customer_phone": conversation[1],
        "state": conversation[2],
        "last_message_at": conversation[3],
        "messages": [
            {"direction": row[0], "sender": row[1], "body": row[2], "created_at": row[3]}
            for row in messages
        ],
    }


def daily_operations_summary() -> Dict[str, int]:
    """Argentina-local daily counts, reusable by the 21:00 report and dashboard."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM conversations
                     WHERE (last_message_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date),
                    (SELECT count(*) FROM pending_actions
                     WHERE status = 'pending'),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'purchase_review' AND status = 'approved'
                       AND (resolved_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date),
                    (SELECT count(*) FROM tiendanube_webhook_events
                     WHERE event_name = 'order/paid' AND status = 'processed'
                       AND (received_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date)
                """
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    return {
        "conversations": int(row[0]),
        "pending": int(row[1]),
        "approved_checkouts": int(row[2]),
        "paid_orders": int(row[3]),
    }


def claim_daily_operations_report(report_day) -> bool:
    """Reserve a calendar-day report so deployments cannot send it twice."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                INSERT INTO fred_daily_reports (report_day, status)
                VALUES (%s, 'sending')
                ON CONFLICT (report_day) DO NOTHING
                """,
                (report_day,),
            )
            claimed = cursor.rowcount == 1
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_daily_operations_report(report_day, sent: bool) -> None:
    """Mark a report sent, or release the date so a failed send can retry."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            if sent:
                cursor.execute(
                    """
                    UPDATE fred_daily_reports
                    SET status = 'sent', sent_at = now()
                    WHERE report_day = %s
                    """,
                    (report_day,),
                )
            else:
                cursor.execute("DELETE FROM fred_daily_reports WHERE report_day = %s", (report_day,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
