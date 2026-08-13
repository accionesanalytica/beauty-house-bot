"""Pure routing policy shared by production and the read-only shadow harness.

This module must stay free of databases, HTTP calls and side effects. The LLM
proposes; verified knowledge obligations and existing hard business boundaries
decide whether the turn can remain with Fred.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Mapping, Optional, Sequence


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", value.lower()).split())


def legacy_special_sale_context(message_text: str, prior_history: Sequence[Mapping[str, Any]]) -> bool:
    """Preserve the old safety boundary when reviewed knowledge is unavailable."""
    normalized_current = _normalise(message_text)
    special_words = r"\b(encargo|encargar|preventa|mayorista|cotizacion)\b"
    if re.search(special_words, normalized_current):
        return True
    last_assistant_text = next(
        (
            _normalise(item.get("content", ""))
            for item in reversed(prior_history or [])
            if item.get("role") == "assistant"
        ),
        "",
    )
    is_short_affirmation = bool(re.fullmatch(
        r"(?:si|si porfa|dale|ok|perfecto)", normalized_current.strip()
    ))
    return bool(is_short_affirmation and re.search(special_words, last_assistant_text))


def lifting_clarification_reply(text: str) -> str:
    """Current production-safe pre-route, exposed so shadow evaluates it too."""
    if "lifting" not in _normalise(text):
        return ""
    return (
        "Para orientarte bien sin prometerte algo que no esté confirmado: "
        "¿tenés el nombre exacto o el link del modelo que te gustaría usar? "
        "Si todavía no lo elegiste, contame si buscás pestañas de banda o cluster "
        "y te ayudo a ubicar opciones 😊"
    )


def resolve_harness_routing(
    message_text: str,
    prior_history: Sequence[Mapping[str, Any]],
    *,
    decision: Optional[Mapping[str, Any]] = None,
    handoff: Optional[Mapping[str, Any]] = None,
    knowledge_retrieval: Any = None,
    dynamic_requirements: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Resolve one routing result from the same evidence in app and shadow."""
    effective_decision = dict(decision or {})
    effective_handoff = dict(handoff or {}) if handoff else None
    governing_topic = getattr(knowledge_retrieval, "governing_topic", None)
    obligations = getattr(knowledge_retrieval, "obligations", None)

    unavailable_requiring_isa = next((
        item for item in dynamic_requirements
        if getattr(item, "status", "") in {"unavailable_tool", "failed"}
        and "isa" in _normalise(getattr(item, "customer_fallback", ""))
    ), None)

    if governing_topic and obligations and obligations.escalation_required:
        reason = (
            "special_sale_request"
            if legacy_special_sale_context(message_text, prior_history)
            else "unable_to_verify"
        )
        effective_handoff = {
            "reason": reason,
            "summary": "El topic aprobado requiere revisión de Isa para este caso.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": reason,
            "summary": "Routing determinado por obligaciones del topic primario.",
        }
        source = "primary_topic_obligation"
    elif unavailable_requiring_isa:
        # If the approved fallback explicitly requires Isa, that next step is
        # routing state, not merely optional wording in the final response.
        effective_handoff = {
            "reason": "unable_to_verify",
            "summary": "El dato live requerido no tiene verificador disponible.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": "unable_to_verify",
            "summary": "Routing determinado por un requerimiento dinámico no verificable.",
        }
        source = "dynamic_requirement_fallback"
    elif not governing_topic and legacy_special_sale_context(message_text, prior_history):
        # Knowledge can be disabled or fail. In that degraded mode, retain the
        # established safety boundary instead of allowing a normal checkout.
        effective_handoff = {
            "reason": "special_sale_request",
            "summary": "Caso especial sin topic aprobado confiable.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": "special_sale_request",
            "summary": "Fallback conservador de venta especial.",
        }
        source = "legacy_safety_fallback"
    else:
        source = "agent_decision"

    return {
        "decision": effective_decision,
        "handoff": effective_handoff,
        "source": source,
        "governing_topic": governing_topic,
    }


def visible_routing_contract(
    routing: Mapping[str, Any],
    *,
    dynamic_requirements: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Describe what the customer-facing reply must communicate.

    This is deliberately domain-neutral. It translates the effective routing
    state into a small language contract instead of maintaining a second set
    of business decisions in the response layer.
    """
    decision = dict(routing.get("decision") or {})
    handoff = dict(routing.get("handoff") or {}) if routing.get("handoff") else None
    action = "handoff_to_isa" if handoff else str(decision.get("action") or "reply")
    reason = str((handoff or {}).get("reason") or decision.get("reason") or "normal_response")
    missing = []
    unavailable = []
    unavailable_messages = []
    for requirement in dynamic_requirements:
        status = getattr(requirement, "status", "")
        if status == "missing_arguments":
            missing.extend(getattr(requirement, "missing_arguments", ()) or ())
        elif status in {"unavailable_tool", "failed"}:
            unavailable.append(getattr(requirement, "fact", ""))
            message = str(getattr(requirement, "customer_fallback", "") or "")
            if message:
                unavailable_messages.append(message)
    return {
        "action": action,
        "reason": reason,
        # Presentation metadata only. These fields do not alter routing; they
        # let the final style layer preserve a genuinely necessary discovery
        # question when no product could be identified.
        "response_mode": str(decision.get("response_mode") or ""),
        "match_type": str(decision.get("match_type") or ""),
        # Which live checks actually returned a verified Tiendanube fact this
        # turn (set by agent.py only after a real get_stock/get_product_
        # availability call succeeded). This is the evidence that a discovery
        # answer is grounded, not merely a syntactically valid decision.
        "checks_completed": list(decision.get("checks_completed") or ()),
        # What this specific decision itself said it needed verified — the
        # other half of the grounded-discovery evidence. "Something got
        # checked" is not the same claim as "what this decision needed got
        # checked"; both fields are required to tell them apart.
        "required_checks": list(decision.get("required_checks") or ()),
        "next_step": (
            "isa_review" if action == "handoff_to_isa"
            else "provide_missing_information" if missing
            else "clarify" if action.startswith("clarify")
            else "continue"
        ),
        "missing_information": list(dict.fromkeys(item for item in missing if item)),
        "unavailable_facts": list(dict.fromkeys(item for item in unavailable if item)),
        "unavailable_messages": list(dict.fromkeys(
            item for item in unavailable_messages if item
        )),
    }


def align_reply_with_routing(
    reply: str,
    routing: Mapping[str, Any],
    *,
    dynamic_requirements: Sequence[Any] = (),
) -> str:
    """Make visible language compatible with the already-resolved action.

    The original answer remains available for useful domain guidance. This
    function only adds or replaces the routing/next-step layer; it never makes
    a new business decision.
    """
    contract = visible_routing_contract(
        routing, dynamic_requirements=dynamic_requirements
    )
    labels = {
        "order_number": "el número de orden",
        "sku": "el modelo exacto o su link",
    }
    missing = [labels.get(item, item.replace("_", " ")) for item in contract["missing_information"]]
    base = str(reply or "").strip()

    if contract["action"] == "handoff_to_isa":
        if missing:
            return (
                "Para verificar bien este caso antes de que lo revise Isa, me falta {}. "
                "Con ese dato se lo paso junto con todo el contexto."
            ).format(" y ".join(missing))
        if contract["unavailable_messages"]:
            base = " ".join(contract["unavailable_messages"])
        clauses = []
        if "isa" not in _normalise(base):
            reasons = {
                "special_sale_request": "Este caso necesita una validación comercial de Isa.",
                "unable_to_verify": "No pude confirmar de forma segura todo lo necesario, así que lo revisa Isa.",
                "safety_concern": "Por seguridad, este caso necesita que lo revise Isa.",
            }
            clauses.append(reasons.get(contract["reason"], "Este caso necesita que lo revise Isa."))
        clauses.append("Se lo paso con lo que ya me contaste y seguimos por acá.")
        return "\n\n".join(part for part in (base, " ".join(clauses)) if part).strip()

    if missing:
        # A product-discovery turn that already resolved a real match against
        # verified Tiendanube facts must not be thrown away just because the
        # generic live-check gate also asked for a bare "sku" argument — the
        # product was already found and verified, there is nothing left to
        # ask the customer. This is deliberately narrow: it only waives the
        # "sku" argument (never order_number or anything else), only when the
        # agent's own decision says product_discovery, only when it landed on
        # an actual match (not "no_match"), and only when a live check truly
        # completed this turn (checks_completed is never set from free-form
        # model text — see agent.py). A lookup or a genuine ambiguity has no
        # completed check to point to, so this never bypasses those.
        #
        # required_checks <= checks_completed, not merely bool(checks_completed):
        # "something was checked" is not the same claim as "what this decision
        # said it needed got checked". A decision that only proved live_stock
        # (e.g. via search_available_products) while still owing live_price
        # (the customer asked for a price) must not be treated as grounded —
        # required_checks is exactly the set that would let that distinction
        # through a bare truthiness check.
        grounded_discovery = (
            set(contract["missing_information"]) <= {"sku"}
            and contract["response_mode"] == "product_discovery"
            and contract["match_type"] not in ("", "no_match")
            and set(contract["required_checks"]) <= set(contract["checks_completed"])
        )
        if not grounded_discovery:
            # A missing live-check argument is the only useful next step.
            # Avoid a free-form answer that asks several unrelated questions.
            return "Para poder verificarlo en vivo, me falta {}.".format(" y ".join(missing))

    if contract["unavailable_messages"]:
        if contract["action"].startswith("clarify"):
            clarification = "Para seguir, necesito que me aclares el modelo exacto o me pases su link."
            return "\n\n".join(part for part in (
                base,
                " ".join(contract["unavailable_messages"]),
                clarification,
            ) if part).strip()
        return "\n\n".join(
            part for part in (base, " ".join(contract["unavailable_messages"])) if part
        ).strip()

    if contract["action"].startswith("clarify"):
        clarification = "Para seguir, necesito que me aclares el modelo exacto o me pases su link."
        if _normalise(clarification) not in _normalise(base):
            return "\n\n".join(part for part in (base, clarification) if part).strip()

    return base
