"""Closed, reusable tool surface for Fred v2.

This module adapts the integrations that v1 already owns.  It deliberately
contains no intent classifier: the agent decides *which* tool it needs, while
these adapters validate identifiers and return evidence from the real source.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable, Dict


MAX_QUERY_CHARS = 160
MAX_SUMMARY_CHARS = 320
ALLOWED_HANDOFF_REASONS = {
    "human_request",
    "purchase_intent",
    "product_advice",
    "unable_to_verify",
}


def _bounded_text(value: Any, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{} es obligatorio".format(field))
    if len(text) > limit:
        raise ValueError("{} supera {} caracteres".format(field, limit))
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("{} contiene caracteres inválidos".format(field))
    return text


def _validated_order_number(value: Any) -> str:
    number = _bounded_text(value, field="order_number", limit=64)
    if not all(char.isalnum() or char == "-" for char in number):
        raise ValueError("order_number inválido")
    return number


def _default_knowledge_search(query: str) -> Dict[str, Any]:
    # Importing app here reuses its source selection (local/Supabase + fallback)
    # without making v2 another owner of that integration.
    from app import search_knowledge_bundle

    retrieval = search_knowledge_bundle(query)
    return {
        "found": bool(retrieval.rows),
        "context": retrieval.context,
        "governing_topic": retrieval.governing_topic,
        "retrieved_topics": list(retrieval.retrieved_topics),
        "obligations": asdict(retrieval.obligations),
        "dynamic_requirements": [asdict(item) for item in retrieval.dynamic_requirements],
    }


def _default_get_order(order_number: str) -> Dict[str, Any]:
    from tiendanube_tools import get_order_status

    return get_order_status(order_number)


def _default_get_product(query: str) -> Dict[str, Any]:
    from tiendanube_tools import get_product_availability, search_products

    candidates = search_products(query, limit=5)
    products = []
    for candidate in candidates:
        product_id = candidate.get("product_id")
        if product_id is None:
            continue
        live = get_product_availability(product_id)
        if live.get("found"):
            products.append(live)
    return {
        "found": bool(products),
        "query": query,
        "products": products,
        "identity_source": "tiendanube",
        "availability_source": "tiendanube_live",
    }


def _default_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    # The first slice is not wired to the webhook.  Persisting/notifying is an
    # explicit later opt-in so a local harness can never contact Isa.
    return {
        "accepted": True,
        "mode": "preview",
        "reason": payload["reason"],
        "summary": payload["summary"],
    }


def live_handoff_adapter(
    *,
    conversation_id: int,
    customer_phone: str,
    customer_message: str,
    conversation_context: list,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the later, explicit opt-in adapter over v1's state/WhatsApp path.

    Merely constructing v2 does not call or import the production app.  A future
    webhook integration must deliberately supply this adapter.
    """
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
            "mode": "live",
            "notified": bool(notified),
            "reason": payload["reason"],
        }

    return handoff


class V2ToolAdapters:
    """The only four domain tools visible to the v2 model."""

    def __init__(
        self,
        *,
        knowledge_search: Callable[[str], Dict[str, Any]] = _default_knowledge_search,
        order_lookup: Callable[[str], Dict[str, Any]] = _default_get_order,
        product_lookup: Callable[[str], Dict[str, Any]] = _default_get_product,
        handoff: Callable[[Dict[str, Any]], Dict[str, Any]] = _default_handoff,
    ) -> None:
        self._knowledge_search = knowledge_search
        self._order_lookup = order_lookup
        self._product_lookup = product_lookup
        self._handoff = handoff

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name == "search_knowledge":
            query = _bounded_text(arguments.get("query"), field="query", limit=MAX_QUERY_CHARS)
            return self._knowledge_search(query)
        if name == "get_order":
            return self._order_lookup(_validated_order_number(arguments.get("order_number")))
        if name == "get_product":
            query = _bounded_text(arguments.get("query"), field="query", limit=MAX_QUERY_CHARS)
            return self._product_lookup(query)
        if name == "handoff_to_isa":
            reason = _bounded_text(arguments.get("reason"), field="reason", limit=64)
            if reason not in ALLOWED_HANDOFF_REASONS:
                raise ValueError("reason de handoff inválido")
            summary = _bounded_text(
                arguments.get("summary"), field="summary", limit=MAX_SUMMARY_CHARS,
            )
            return self._handoff({"reason": reason, "summary": summary})
        raise ValueError("Herramienta no permitida: {}".format(name))


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Busca políticas y procedimientos aprobados de Beauty House.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Consulta un pedido real por su número y devuelve fulfillment live.",
            "parameters": {
                "type": "object",
                "properties": {"order_number": {"type": "string"}},
                "required": ["order_number"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product",
            "description": "Identifica productos reales y consulta variantes/stock live.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_isa",
            "description": "Deriva a Isa compras, asesoramiento o casos no verificables. No crea checkout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": sorted(ALLOWED_HANDOFF_REASONS),
                    },
                    "summary": {"type": "string"},
                },
                "required": ["reason", "summary"],
                "additionalProperties": False,
            },
        },
    },
]
