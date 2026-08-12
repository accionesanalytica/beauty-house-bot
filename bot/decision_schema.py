"""Validated decision envelope between Fred's model and application code.

The LLM can propose an action, but it never authorizes a commercial action by
itself. This module derives the effective action from verifiable facts produced
by tools in the same turn.
"""

from __future__ import annotations

import re
import unicodedata
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
VALID_RESPONSE_MODES = {
    "general_response",
    "product_discovery",
    "product_advice",
    "policy_answer",
}
VALID_MATCH_TYPES = {
    "exact_match",
    "close_alternative",
    "same_brand_other_category",
    "no_match",
}
VALID_REQUIRED_CHECKS = {"live_stock", "live_price"}
MAX_SUMMARY_CHARS = 360
MAX_PRODUCT_LABEL_CHARS = 180


def _normalize_product_type(value: str) -> str:
    """Normalize a model-provided canonical commercial product type."""
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def validate_model_decision(payload: Any) -> Optional[Dict[str, Any]]:
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
    decision = {"action": action, "reason": reason, "summary": summary}

    response_mode = str(payload.get("response_mode") or "").strip()
    match_type = str(payload.get("match_type") or "").strip()
    requested_product = " ".join(str(payload.get("requested_product") or "").split())
    matched_product = " ".join(str(payload.get("matched_product") or "").split())
    requested_product_type = " ".join(str(payload.get("requested_product_type") or "").split())
    matched_product_type = " ".join(str(payload.get("matched_product_type") or "").split())
    required_checks = payload.get("required_checks") or []

    # Extra commercial fields are optional for backwards compatibility, but
    # once one is present the envelope must be internally complete. This keeps
    # a normal support reply small while making product discovery auditable.
    has_product_envelope = any((
        response_mode,
        match_type,
        requested_product,
        matched_product,
        requested_product_type,
        matched_product_type,
        required_checks,
    ))
    if not has_product_envelope:
        return decision
    if response_mode not in VALID_RESPONSE_MODES:
        return None
    if response_mode == "product_discovery":
        if (
            match_type not in VALID_MATCH_TYPES
            or not requested_product
            or not requested_product_type
        ):
            return None
        if match_type != "no_match" and (not matched_product or not matched_product_type):
            return None
    elif match_type and match_type not in VALID_MATCH_TYPES:
        return None
    if (
        len(requested_product) > MAX_PRODUCT_LABEL_CHARS
        or len(matched_product) > MAX_PRODUCT_LABEL_CHARS
        or len(requested_product_type) > MAX_PRODUCT_LABEL_CHARS
        or len(matched_product_type) > MAX_PRODUCT_LABEL_CHARS
        or not isinstance(required_checks, list)
        or any(item not in VALID_REQUIRED_CHECKS for item in required_checks)
    ):
        return None
    # The model classifies the semantic relation, but code enforces the basic
    # commercial invariant: two different canonical product types cannot be an
    # exact match. This is category-agnostic and prevents a related accessory,
    # format or sibling product from silently becoming the requested product.
    if (
        match_type == "exact_match"
        and _normalize_product_type(requested_product_type)
        != _normalize_product_type(matched_product_type)
    ):
        match_type = "close_alternative"

    decision.update({
        "response_mode": response_mode,
        "match_type": match_type,
        "requested_product": requested_product,
        "matched_product": matched_product,
        "requested_product_type": requested_product_type,
        "matched_product_type": matched_product_type,
        "required_checks": list(dict.fromkeys(required_checks)),
    })
    return decision


def build_effective_decision(
    proposed: Optional[Dict[str, Any]],
    *,
    sale_candidate: Optional[Dict[str, Any]] = None,
    handoff: Optional[Dict[str, Any]] = None,
    needs_product_clarification: bool = False,
) -> Dict[str, Any]:
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
