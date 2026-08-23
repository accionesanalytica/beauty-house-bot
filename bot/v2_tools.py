"""Closed, reusable tool surface for Fred v2.

This module adapts the integrations that v1 already owns.  It deliberately
contains no intent classifier: the agent decides *which* tool it needs, while
these adapters validate identifiers and return evidence from the real source.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict


MAX_QUERY_CHARS = 160
MAX_SUMMARY_CHARS = 320
ALLOWED_HANDOFF_REASONS = {
    "custom_order",
    "human_request",
    "purchase_intent",
    "product_advice",
    "unable_to_verify",
    "cancel_order",
    "modify_order",
    "return_order",
    "sensitive_order_action",
    "operational_detail_unverified",
}
ALLOWED_HANDOFF_OPERATIONS = {"create", "repeat", "resolve"}


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
    if number.startswith("#"):
        number = number[1:].strip()
    if not all(char.isalnum() or char == "-" for char in number):
        raise ValueError("order_number inválido")
    return number


def _verified_price(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ""
    if amount < 0:
        return ""
    rendered = format(amount, ",.2f")
    whole, decimals = rendered.split(".")
    whole = whole.replace(",", ".")
    return "${}".format(whole if decimals == "00" else "{},{}".format(whole, decimals))


def _product_safe_reply(products: Any) -> str:
    lines = ["Esto es lo que figura ahora en Tiendanube:"]
    for product in list(products or [])[:3]:
        product_name = str(product.get("product_name") or "Producto")[:120]
        variants = list(product.get("variants") or [])[:5]
        if not variants:
            lines.append("- {}: sin variantes publicadas para informar.".format(product_name))
            continue
        for variant in variants:
            details = []
            variant_name = str(variant.get("variant") or "").strip()[:80]
            status = str(variant.get("status") or "")
            quantity = variant.get("quantity")
            if status == "in_stock":
                if isinstance(quantity, int) and not isinstance(quantity, bool):
                    details.append("{} {}".format(
                        quantity, "unidad" if quantity == 1 else "unidades",
                    ))
                else:
                    details.append("stock disponible")
            elif status == "out_of_stock":
                details.append("sin stock")
            else:
                details.append("stock no confirmado")
            price = _verified_price(variant.get("price"))
            if price:
                details.append(price)
            label = "{} ({})".format(product_name, variant_name) if variant_name else product_name
            lines.append("- {}: {}.".format(label, ", ".join(details)))
    return "\n".join(lines)


def _without_purchase_links(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_purchase_links(item)
            for key, item in value.items()
            if str(key).lower() not in {"product_url", "checkout_url"}
        }
    if isinstance(value, list):
        return [_without_purchase_links(item) for item in value]
    return value


def _default_knowledge_search(query: str) -> Dict[str, Any]:
    # Importing app here reuses its source selection (local/Supabase + fallback)
    # without making v2 another owner of that integration.
    from app import search_knowledge_bundle

    retrieval = search_knowledge_bundle(query)
    found = bool(retrieval.rows)
    handoff_required = bool(retrieval.obligations.escalation_required) or not found
    return {
        "found": found,
        "status": "found" if found else "not_found",
        "context": retrieval.context,
        "governing_topic": retrieval.governing_topic,
        "retrieved_topics": list(retrieval.retrieved_topics),
        "obligations": asdict(retrieval.obligations),
        "handoff_required": handoff_required,
        "allowed_next_action": "handoff_to_isa" if handoff_required else "reply",
        "dynamic_requirements": [asdict(item) for item in retrieval.dynamic_requirements],
    }


def _default_get_order(order_number: str) -> Dict[str, Any]:
    from tiendanube_tools import get_order_status

    return get_order_status(order_number)


def _normalise_order_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the audited customer meaning of Tiendanube fulfillment fields."""
    if not result.get("found"):
        return {**result, "status": "not_found", "allowed_next_action": "handoff_to_isa"}
    # Reuse v1's audited renderer rather than introducing a second status map.
    from app import _render_order_status_reply

    fulfillment = str(result.get("fulfillment_status") or "").strip().upper()
    shipping_type = str(result.get("shipping_type") or "").strip().lower()
    semantics = {
        "UNPACKED": "in_preparation",
        "PACKED": "packed_waiting_dispatch" if shipping_type == "ship" else "packed_waiting_pickup_confirmation",
        "DISPATCHED": "dispatched",
        "DELIVERED": "delivered",
    }.get(fulfillment, "in_preparation")
    return {
        **result,
        "status": "found",
        "fulfillment_semantics": semantics,
        "customer_safe_reply": _render_order_status_reply(result),
        "allowed_next_action": "reply_with_customer_safe_reply",
    }


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
            # Fred can report verified identity/price/stock, but the v2
            # cutover does not generate or offer product/purchase links.
            products.append({
                key: value for key, value in live.items()
                if key not in {"product_url", "checkout_url"}
            })
    return {
        "found": bool(products),
        "status": "found" if products else "not_found",
        "query": query,
        "products": products,
        "identity_source": "tiendanube",
        "availability_source": "tiendanube_live",
        "allowed_next_action": (
            "reply_from_live_evidence" if products else "handoff_to_isa/custom_order"
        ),
    }


def _default_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    # The first slice is not wired to the webhook.  Persisting/notifying is an
    # explicit later opt-in so a local harness can never contact Isa.
    return {
        "accepted": True,
        "status": "simulated_success",
        "would_handoff": True,
        "side_effect_executed": False,
        "reason": payload["reason"],
        "summary": payload["summary"],
    }


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
            result = self._knowledge_search(query)
            if not result.get("found"):
                result = {
                    **result,
                    "status": "not_found",
                    "handoff_required": True,
                    "allowed_next_action": "handoff_to_isa",
                }
            elif "allowed_next_action" not in result:
                result = {
                    **result,
                    "allowed_next_action": (
                        "handoff_to_isa" if result.get("handoff_required") else "reply"
                    ),
                }
            return result
        if name == "get_order":
            return _normalise_order_result(
                self._order_lookup(_validated_order_number(arguments.get("order_number")))
            )
        if name == "get_product":
            query = _bounded_text(arguments.get("query"), field="query", limit=MAX_QUERY_CHARS)
            result = _without_purchase_links(self._product_lookup(query))
            if "status" not in result:
                result = {
                    **result,
                    "status": "found" if result.get("found") else "not_found",
                    "allowed_next_action": (
                        "reply_from_live_evidence"
                        if result.get("found") else "handoff_to_isa/custom_order"
                    ),
                }
            if result.get("status") == "found":
                result = {
                    **result,
                    "customer_safe_reply": _product_safe_reply(result.get("products") or []),
                }
            if result.get("status") == "not_found" and "customer_safe_reply" not in result:
                result = {
                    **result,
                    "customer_safe_reply": (
                        "No aparece publicado en nuestra web. Isa puede revisar si se puede "
                        "conseguir por encargo."
                    ),
                }
            return result
        if name == "handoff_to_isa":
            reason = _bounded_text(arguments.get("reason"), field="reason", limit=64)
            if reason not in ALLOWED_HANDOFF_REASONS:
                raise ValueError("reason de handoff inválido")
            operation = str(arguments.get("operation") or "create").strip().lower()
            if operation not in ALLOWED_HANDOFF_OPERATIONS:
                raise ValueError("operation de handoff inválida")
            summary = _bounded_text(
                arguments.get("summary"), field="summary", limit=MAX_SUMMARY_CHARS,
            )
            payload = {"operation": operation, "reason": reason, "summary": summary}
            if arguments.get("order_number"):
                payload["order_number"] = _validated_order_number(arguments.get("order_number"))
            if operation in {"repeat", "resolve"}:
                try:
                    handoff_id = int(arguments.get("handoff_id") or 0)
                except (TypeError, ValueError) as error:
                    raise ValueError("handoff_id inválido") from error
                if handoff_id <= 0:
                    raise ValueError("handoff_id inválido")
                payload["handoff_id"] = handoff_id
            return self._handoff(payload)
        raise ValueError("Herramienta no permitida: {}".format(name))


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "Busca políticas y procedimientos aprobados de Beauty House. "
                "Si allowed_next_action=reply, respondé y NO hagas handoff."
            ),
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
            "description": (
                "Consulta un pedido real por su número. Devuelve customer_safe_reply, "
                "la interpretación cerrada y auditada de fulfillment; no reinterpretar PACKED."
            ),
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
            "description": (
                "Sólo para un producto concreto nombrado por la clienta: identifica producto, "
                "SKU, variantes, stock y precio live. Nunca usar para gustos, necesidades, "
                "comparaciones o recomendaciones. Si status=not_found, la única acción siguiente "
                "es handoff_to_isa con reason=custom_order."
            ),
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
            "description": (
                "Gestiona un handoff por tema mediante un link que decide abrir la clienta; nunca "
                "envía un mensaje a Isa. Es OBLIGATORIA para asesoramiento subjetivo "
                "(reason=product_advice), "
                "cualquier solicitud explícita de comprar/llevar una cantidad "
                "(reason=purchase_intent), aun sin identidad verificada, y producto no encontrado "
                "(custom_order). También es obligatoria con reason=human_request para pedidos "
                "de hablar con una persona. Usar cancel_order, modify_order, return_order o "
                "sensitive_order_action para acciones sensibles sobre pedidos, y "
                "operational_detail_unverified si Knowledge no confirma un procedimiento. "
                "operation=create inicia; repeat vuelve a mostrar el link sin afirmar qué pasó "
                "con Isa; resolve sólo cuando la clienta confirma que ya habló con ella. Para "
                "compra no consultar catálogo ni pedir foto/link. No crea checkout ni modifica pedidos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": sorted(ALLOWED_HANDOFF_REASONS),
                    },
                    "summary": {"type": "string"},
                    "operation": {
                        "type": "string",
                        "enum": sorted(ALLOWED_HANDOFF_OPERATIONS),
                    },
                    "handoff_id": {"type": "integer", "minimum": 1},
                    "order_number": {"type": "string"},
                },
                "required": ["reason", "summary"],
                "additionalProperties": False,
            },
        },
    },
]
