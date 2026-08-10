"""Validated decision envelope between Fred's model and application code.

The LLM can propose an action, but it never authorizes a commercial action by
itself. This module derives the effective action from verifiable facts produced
by tools in the same turn.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


VALID_ACTIONS = {
    "reply",
    "clarify_product",
    "start_sales_intake",
    "handoff_to_isa",
}
VALID_REASONS = {
    "normal_response",
    "product_ambiguity",
    "human_request",
    "purchase_intent",
    "special_sale_request",
    "unable_to_verify",
}
MAX_SUMMARY_CHARS = 360


def validate_model_decision(payload: Any) -> Optional[Dict[str, str]]:
    """Validate a proposed model decision without raising on malformed output."""
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    summary = " ".join(str(payload.get("summary") or "").split())
    if action not in VALID_ACTIONS or reason not in VALID_REASONS:
        return None
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        return None
    return {"action": action, "reason": reason, "summary": summary}


def build_effective_decision(
    proposed: Optional[Dict[str, str]],
    *,
    sale_candidate: Optional[Dict[str, Any]] = None,
    handoff: Optional[Dict[str, Any]] = None,
    needs_product_clarification: bool = False,
) -> Dict[str, str]:
    """Derive a safe executable action from tool-confirmed facts.

    Precedence is deliberate: a verified sale selection or an explicit handoff
    overrides a free-text proposal. A malformed or contradictory proposal
    degrades to normal reply/clarification, never to a commercial action.
    """
    proposed = validate_model_decision(proposed)
    if sale_candidate and sale_candidate.get("sku") and sale_candidate.get("product_name"):
        return {
            "action": "start_sales_intake",
            "reason": "purchase_intent",
            "summary": "Variante verificada para ficha de compra.",
        }
    if handoff and handoff.get("reason"):
        reason = str(handoff["reason"])
        if reason not in VALID_REASONS:
            reason = "unable_to_verify"
        return {
            "action": "handoff_to_isa",
            "reason": reason,
            "summary": " ".join(str(handoff.get("summary") or "Se requiere revisión de Isa.").split())[:MAX_SUMMARY_CHARS],
        }
    if needs_product_clarification:
        return {
            "action": "clarify_product",
            "reason": "product_ambiguity",
            "summary": "Falta identificar el producto con seguridad.",
        }
    if proposed and proposed["action"] in {"reply", "clarify_product"}:
        return proposed
    return {
        "action": "reply",
        "reason": "normal_response",
        "summary": "Respuesta comercial sin acción operativa.",
    }
