"""Explicit future opt-in adapters with side effects; never imported by shadow."""

from typing import Any, Callable, Dict


def live_handoff_adapter(
    *,
    conversation_id: int,
    customer_phone: str,
    customer_message: str,
    conversation_context: list,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
        from app import _queue_for_isa

        notified = _queue_for_isa(
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            action_type=(
                "purchase_review" if payload["reason"] == "purchase_intent"
                else "human_handoff"
            ),
            summary=payload["summary"],
            customer_message=customer_message,
            conversation_context=conversation_context,
        )
        return {
            "accepted": True,
            "status": "executed",
            "would_handoff": True,
            "side_effect_executed": True,
            "notified": bool(notified),
            "reason": payload["reason"],
        }

    return handoff
