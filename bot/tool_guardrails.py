"""Deterministic boundaries for model-initiated tool calls and replies."""

import re
from typing import Any, Dict, Optional, Tuple

from decision_schema import VALID_ACTIONS, VALID_REASONS, validate_model_decision


MAX_TOOL_CALLS_PER_ROUND = 4
MAX_TOOL_CALLS_PER_TURN = 8
MAX_TOOL_QUERY_CHARS = 160
MAX_SKU_CHARS = 80
MAX_CUSTOMER_REPLY_CHARS = 1500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ORDER_NUMBER = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _clean_text(value: Any, maximum: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = _CONTROL_CHARS.sub("", value).strip()
    if not value or len(value) > maximum:
        return None
    return value


def _bounded_limit(value: Any, default: int = 5) -> Optional[int]:
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 5 else None


def validate_tool_arguments(name: str, arguments: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return sanitized arguments or a customer-safe rejection reason."""
    if not isinstance(arguments, dict):
        return None, "Argumentos inválidos. Pedí una aclaración sin adivinar."

    if name in {"search_products", "search_available_products"}:
        query = _clean_text(arguments.get("query"), MAX_TOOL_QUERY_CHARS)
        limit = _bounded_limit(arguments.get("limit"))
        if query is None or len(query) < 2 or limit is None:
            return None, "Búsqueda inválida. Pedí el nombre del producto de forma breve."
        return {"query": query, "limit": limit}, None

    if name == "get_stock":
        sku = _clean_text(arguments.get("sku"), MAX_SKU_CHARS)
        if sku is None or "\n" in sku or "\r" in sku:
            return None, "SKU inválido. No afirmes disponibilidad."
        return {"sku": sku}, None

    if name == "get_product_availability":
        product_id = arguments.get("product_id")
        if isinstance(product_id, bool):
            return None, "Identificador de producto inválido."
        try:
            product_id = int(product_id)
        except (TypeError, ValueError):
            return None, "Identificador de producto inválido."
        if not 1 <= product_id <= 10**12:
            return None, "Identificador de producto inválido."
        return {"product_id": product_id}, None

    if name == "get_order_status":
        order_number = _clean_text(arguments.get("order_number"), 64)
        if order_number is None or not _ORDER_NUMBER.fullmatch(order_number):
            return None, "Número de orden inválido. No infieras el estado del pedido."
        return {"order_number": order_number}, None

    if name == "request_isa_handoff":
        reason = str(arguments.get("reason") or "").strip()
        summary = _clean_text(arguments.get("summary"), 360)
        if reason not in VALID_REASONS - {"normal_response", "product_ambiguity"} or summary is None:
            return None, "Escalación inválida. Pedí una aclaración o respondé con información verificada."
        return {"reason": reason, "summary": summary}, None

    if name == "select_sale_candidate":
        sku = _clean_text(arguments.get("sku"), MAX_SKU_CHARS)
        product_name = _clean_text(arguments.get("product_name"), 180)
        variant = _clean_text(arguments.get("variant"), 180)
        if sku is None or product_name is None or variant is None:
            return None, "Selección de venta inválida. No inicies una compra."
        return {"sku": sku, "product_name": product_name, "variant": variant}, None

    if name == "set_turn_decision":
        decision = validate_model_decision(arguments)
        if decision is None:
            return None, "Decisión inválida. Cerrá con una respuesta segura."
        return decision, None

    return None, "Herramienta no permitida."


def bounded_customer_reply(text: Any) -> str:
    """Avoid very long, malformed WhatsApp responses while keeping a sentence."""
    text = _CONTROL_CHARS.sub("", str(text or "")).strip()
    if len(text) <= MAX_CUSTOMER_REPLY_CHARS:
        return text
    prefix = text[:MAX_CUSTOMER_REPLY_CHARS]
    cut = max(prefix.rfind(". "), prefix.rfind("! "), prefix.rfind("? "))
    if cut >= MAX_CUSTOMER_REPLY_CHARS // 2:
        return prefix[:cut + 1].strip()
    return prefix.rstrip() + "…"
