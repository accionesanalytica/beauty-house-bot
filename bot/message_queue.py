"""Pure helpers for durable WhatsApp turn orchestration.

The database owns scheduling and leases.  This module deliberately contains
only payload parsing and small time calculations so the important behaviour is
deterministic and cheap to benchmark without Meta, Supabase or an LLM.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


def seed_last_processed_message_id(
    prior_customer_message_ids: Iterable[int], new_message_id: int
) -> int:
    """Watermark for a conversation entering the durable system for the first time.

    Reference semantics for the ``conversation_processing`` seed subquery in
    ``conversation_store.enqueue_inbound_message``: any customer message that
    already existed before ``new_message_id`` is treated as already processed,
    so only ``new_message_id`` and later messages are pending work. A brand
    new conversation has no prior ids, so the watermark stays at 0 and its
    first message is processed normally.
    """
    prior = [message_id for message_id in prior_customer_message_ids if message_id < new_message_id]
    return max(prior, default=0)


@dataclass(frozen=True)
class InboundWhatsAppMessage:
    phone: str
    wa_message_id: str
    text: str
    provider_timestamp: Optional[datetime]
    interactive_reply_id: str = ""


def _provider_datetime(raw_value: Any) -> Optional[datetime]:
    if raw_value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(raw_value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def extract_inbound_messages(payload: Dict[str, Any]) -> List[InboundWhatsAppMessage]:
    """Return every supported inbound message in a Meta webhook payload.

    Meta may batch entries, changes and messages.  Status notifications and
    unsupported media are ignored here; later phases can add media extraction
    without changing queue semantics.
    """
    extracted: List[InboundWhatsAppMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                interactive = message.get("interactive") or {}
                text = ((message.get("text") or {}).get("body") or "").strip()
                if not text:
                    text = (
                        ((interactive.get("button_reply") or {}).get("title") or "")
                        or ((interactive.get("list_reply") or {}).get("title") or "")
                    ).strip()
                phone = str(message.get("from") or "").strip()
                message_id = str(message.get("id") or "").strip()
                if not phone or not message_id or not text:
                    continue
                extracted.append(
                    InboundWhatsAppMessage(
                        phone=phone,
                        wa_message_id=message_id,
                        text=text,
                        provider_timestamp=_provider_datetime(message.get("timestamp")),
                        interactive_reply_id=(
                            ((interactive.get("button_reply") or {}).get("id") or "")
                            or ((interactive.get("list_reply") or {}).get("id") or "")
                        ).strip(),
                    )
                )
    return extracted


def ordered_turn_text(messages: Iterable[Dict[str, Any]]) -> str:
    """Join an already ordered durable batch as one human turn."""
    return "\n".join(
        str(message.get("body") or "").strip()
        for message in messages
        if str(message.get("body") or "").strip()
    )


def next_process_at(
    first_pending_at: datetime,
    latest_received_at: datetime,
    quiet_seconds: float,
    max_burst_seconds: float,
) -> datetime:
    """Schedule after a quiet period, bounded from the first bubble."""
    quiet_at = latest_received_at + timedelta(seconds=max(0.0, quiet_seconds))
    maximum_at = first_pending_at + timedelta(seconds=max(0.0, max_burst_seconds))
    return min(quiet_at, maximum_at)
