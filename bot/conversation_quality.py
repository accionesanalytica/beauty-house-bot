"""Final WhatsApp presentation layer.

This module does not choose topics, routes, tools or business facts.  It only
turns an already-approved reply and routing contract into concise WhatsApp
copy while preserving URLs and mandatory content.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Optional, Sequence


URL_PATTERN = re.compile(r"https?://[^\s<>()]+")
GENERIC_CTA_PATTERN = re.compile(
    r"(?:\n\s*)*¿(?:quer[eé]s|te gustar[ií]a)\s+(?:que\s+)?(?:te\s+)?"
    r"(?:cuente|ayude|muestre|pase|explique|diga|oriente|revise|busque)\b[^?]*\?\s*$",
    re.IGNORECASE,
)
QUESTION_SENTENCE_PATTERN = re.compile(r"(?:^|(?<=\s))¿[^?\n]*\?", re.MULTILINE)
GENERIC_QUESTION_PATTERN = re.compile(
    r"¿(?:te sirve|necesit[aá]s algo m[aá]s|te qued[oó] alguna duda|"
    r"quer[eé]s que te cuente algo m[aá]s(?: del [^?]+)?)\?",
    re.IGNORECASE,
)


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", value.lower()).split())


def _strip_repeated_greeting(reply: str, history: Sequence[Mapping[str, Any]]) -> str:
    if not any(item.get("role") == "assistant" for item in history or []):
        return reply
    return re.sub(
        r"^\s*[¡!]*(?:hola|buen(?:os|as)?\s+(?:d[ií]as|tardes|noches))"
        r"(?:\s+(?:hermosa|linda))?[!,.\s😊🫶🏻🤍]*",
        "",
        reply,
        count=1,
        flags=re.IGNORECASE,
    ).lstrip()


def _remove_generic_cta(reply: str, contract: Mapping[str, Any]) -> str:
    if contract.get("next_step") in {"provide_missing_information", "clarify", "isa_review"}:
        return reply
    return GENERIC_CTA_PATTERN.sub("", reply).rstrip()


def _remove_unneeded_questions(reply: str, contract: Mapping[str, Any]) -> str:
    """Remove model-added questions when routing requires no customer input.

    A question remains mandatory when the routing contract identifies missing
    information or an explicit clarification step.  For an Isa handoff, it is
    also retained when the handoff still needs a concrete argument first.
    """
    next_step = contract.get("next_step")
    missing = contract.get("missing_information") or []
    response_mode = contract.get("response_mode")
    unresolved_discovery = (
        response_mode == "product_discovery"
        and contract.get("match_type") in {"no_match", "close_alternative"}
    )
    if unresolved_discovery:
        return reply
    if next_step == "continue":
        reply = GENERIC_QUESTION_PATTERN.sub("", reply).strip()
    # Legacy/degraded turns may not expose a structured response mode. Keep
    # substantive questions in that case; only generic closing CTAs above are
    # safe to remove without guessing the business stage.
    if next_step == "continue" and not response_mode:
        return reply
    may_remove = next_step == "continue" or (next_step == "isa_review" and not missing)
    if not may_remove:
        return reply
    return QUESTION_SENTENCE_PATTERN.sub("", reply).strip()


def _avoid_repeated_handoff(
    reply: str,
    history: Sequence[Mapping[str, Any]],
    contract: Mapping[str, Any],
) -> str:
    """Advance an Isa handoff instead of repeating the previous turn verbatim."""
    if contract.get("next_step") != "isa_review":
        return reply
    previous = next(
        (str(item.get("content") or "") for item in reversed(history or [])
         if item.get("role") == "assistant"),
        "",
    )
    if previous and _normalise(previous) == _normalise(reply):
        return "Entiendo. Se lo dejo a Isa con todo el contexto para que lo revise y seguimos por acá."
    return reply


def _compact_handoff_copy(reply: str, contract: Mapping[str, Any]) -> str:
    if contract.get("next_step") != "isa_review":
        return reply
    text = re.sub(
        r"Se lo paso a Isa para que lo revise y seguimos por acá\.",
        "Se lo paso con todo el contexto y seguimos por acá."
        if len(re.findall(r"\bIsa\b", reply, flags=re.IGNORECASE)) > 1
        else "Se lo paso a Isa con todo el contexto y seguimos por acá.",
        reply,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?:^|\n)\s*Así te puedo orientar mejor\.\s*(?=\n|$)",
        "\n",
        text,
        flags=re.IGNORECASE,
    ).strip()


def _naturalise_routing_copy(reply: str, contract: Mapping[str, Any]) -> str:
    text = reply
    text = re.sub(
        r"Para poder verificarlo en vivo, me falta el número de orden\.?",
        "¿Me pasás el número de orden? Lo necesito para revisarlo.",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Para verificar bien este caso antes de que lo revise Isa, me falta el número de orden\.\s*"
        r"Con ese dato se lo paso junto con todo el contexto\.?",
        "¿Me pasás el número de orden? Con eso lo reviso y se lo dejo a Isa con todo el contexto.",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Se lo paso con lo que ya me contaste y seguimos por acá\.?",
        "Se lo paso a Isa para que lo revise y seguimos por acá.",
        text,
        flags=re.IGNORECASE,
    )
    return text


def apply_conversation_contract(
    reply: str,
    *,
    history: Sequence[Mapping[str, Any]] = (),
    routing_contract: Optional[Mapping[str, Any]] = None,
) -> str:
    """Polish final copy without changing decisions or commercial evidence."""
    contract = dict(routing_contract or {})
    original = str(reply or "").strip()
    urls_before = URL_PATTERN.findall(original)

    polished = _strip_repeated_greeting(original, history)
    polished = _naturalise_routing_copy(polished, contract)
    polished = _avoid_repeated_handoff(polished, history, contract)
    polished = _compact_handoff_copy(polished, contract)
    polished = _remove_generic_cta(polished, contract)
    polished = _remove_unneeded_questions(polished, contract)
    polished = re.sub(r"[ \t]+\n", "\n", polished)
    polished = re.sub(r" {2,}", " ", polished)
    polished = re.sub(r"\n{3,}", "\n\n", polished).strip()

    # A presentation-only layer may never add, reconstruct or lose a URL.
    if URL_PATTERN.findall(polished) != urls_before:
        return original
    return polished or original
