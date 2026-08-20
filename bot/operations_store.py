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
        CREATE TABLE IF NOT EXISTS fred_turn_observations (
            id BIGSERIAL PRIMARY KEY,
            source_message_id TEXT UNIQUE,
            conversation_id BIGINT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            outcome TEXT NOT NULL,
            tool_names JSONB NOT NULL DEFAULT '[]'::jsonb,
            catalog_context_used BOOLEAN NOT NULL DEFAULT false,
            knowledge_context_used BOOLEAN NOT NULL DEFAULT false,
            model_calls INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS fred_turn_observations_created_idx
        ON fred_turn_observations (created_at DESC)
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


def daily_quality_snapshot() -> Dict[str, int]:
    """Return factual review signals for Fred without invoking any AI service.

    An escalation is not automatically a failure: sometimes it is exactly the
    safe outcome. These counts simply give Isa a short list to inspect and turn
    into future improvements.
    """
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                SELECT
                    (SELECT count(*) FROM pending_actions
                     WHERE status = 'pending'),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'bot_fallback'
                       AND (created_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'human_handoff'
                       AND (created_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'special_sale_request'
                       AND (created_at AT TIME ZONE 'America/Argentina/Buenos_Aires')::date =
                           (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date),
                    (SELECT count(*) FROM pending_actions
                     WHERE action_type = 'purchase_review' AND status = 'pending')
                """
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    return {
        "pending_actions": int(row[0]),
        "bot_fallbacks_today": int(row[1]),
        "human_handoffs_today": int(row[2]),
        "special_sales_today": int(row[3]),
        "pending_purchase_reviews": int(row[4]),
    }


def record_agent_turn(
    *,
    source_message_id: str,
    conversation_id: int,
    action: str,
    reason: str,
    outcome: str,
    tool_names: List[str],
    catalog_context_used: bool,
    knowledge_context_used: bool,
    model_calls: int,
    prompt_tokens: int,
    completion_tokens: int,
    duration_ms: int,
) -> bool:
    """Persist safe turn telemetry once, without customer text or secrets."""
    allowed_actions = {
        "reply", "clarify_product", "start_sales_intake", "handoff_to_isa", "service_fallback",
    }
    allowed_outcomes = {"replied", "queued_for_isa", "send_failed", "service_fallback"}
    if action not in allowed_actions or outcome not in allowed_outcomes:
        raise ValueError("Observación de turno inválida")

    def positive_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    safe_tool_names = [str(name)[:80] for name in tool_names if str(name)[:80]]
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                INSERT INTO fred_turn_observations (
                    source_message_id, conversation_id, action, reason, outcome,
                    tool_names, catalog_context_used, knowledge_context_used,
                    model_calls, prompt_tokens, completion_tokens, duration_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_message_id) DO NOTHING
                """,
                (
                    (source_message_id or None), int(conversation_id), action, str(reason)[:80], outcome,
                    Json(safe_tool_names), bool(catalog_context_used), bool(knowledge_context_used),
                    positive_int(model_calls), positive_int(prompt_tokens),
                    positive_int(completion_tokens), positive_int(duration_ms),
                ),
            )
            inserted = cursor.rowcount == 1
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def agent_observability_snapshot() -> Dict[str, Any]:
    """Read-only operational telemetry for a future owner dashboard."""
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            _ensure_storage(cursor)
            cursor.execute(
                """
                SELECT count(*), COALESCE(round(avg(duration_ms)), 0),
                       COALESCE(round(avg(prompt_tokens + completion_tokens)), 0),
                       count(*) FILTER (WHERE outcome = 'service_fallback')
                FROM fred_turn_observations
                WHERE created_at >= now() - interval '24 hours'
                """
            )
            totals = cursor.fetchone()
            cursor.execute(
                """
                SELECT action, count(*)
                FROM fred_turn_observations
                WHERE created_at >= now() - interval '24 hours'
                GROUP BY action ORDER BY action
                """
            )
            actions = {row[0]: int(row[1]) for row in cursor.fetchall()}
    finally:
        connection.close()
    return {
        "turns": int(totals[0]),
        "average_duration_ms": int(totals[1]),
        "average_tokens": int(totals[2]),
        "service_fallbacks": int(totals[3]),
        "actions": actions,
    }


def record_v2_shadow_observation(observation: Dict[str, Any]) -> bool:
    """Persist an isolated shadow record; never touch conversations or v1 state."""
    if observation.get("side_effects") is not False:
        raise ValueError("Shadow con side effects rechazado")

    def non_negative(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO fred_v2_shadow_observations (
                    correlation_id, source_message_id_hash, conversation_id, generation,
                    input_text_redacted, v1_response_redacted, v2_response_redacted,
                    v1_response_hash, v2_response_hash, v2_tool_calls, v2_tool_results,
                    v2_llm_calls, v2_prompt_tokens, v2_completion_tokens, v2_latency_ms,
                    v2_handoff_reason, error_type, side_effects
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)
                ON CONFLICT (correlation_id) DO NOTHING
                """,
                (
                    str(observation.get("correlation_id") or "")[:80],
                    str(observation.get("source_message_id_hash") or "")[:64] or None,
                    int(observation.get("conversation_id") or 0),
                    non_negative(observation.get("generation")),
                    str(observation.get("input_text_redacted") or "")[:4000],
                    str(observation.get("v1_response_redacted") or "")[:4000],
                    str(observation.get("v2_response_redacted") or "")[:4000],
                    str(observation.get("v1_response_hash") or "")[:64],
                    str(observation.get("v2_response_hash") or "")[:64],
                    Json(observation.get("v2_tool_calls") or []),
                    Json(observation.get("v2_tool_results") or []),
                    non_negative(observation.get("v2_llm_calls")),
                    non_negative(observation.get("v2_prompt_tokens")),
                    non_negative(observation.get("v2_completion_tokens")),
                    non_negative(observation.get("v2_latency_ms")),
                    str(observation.get("v2_handoff_reason") or "")[:80] or None,
                    str(observation.get("error_type") or "")[:120] or None,
                ),
            )
            inserted = cursor.rowcount == 1
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
