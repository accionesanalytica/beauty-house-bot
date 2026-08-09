"""
Tiendanube tools for function calling.

This is the missing piece of Module 7: instead of a simulated
check_order_status, these tools hit the real Tiendanube API.

Architecture:
    RAG (Chroma)  -> identifies WHICH product the customer means (semantic)
    These tools   -> fetch the ACTUAL stock number (live, exact)

The LLM never invents stock. It calls get_stock() and reports what comes back.

Python 3.9 compatible.
"""

import os
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "2025-03"
STORE_ID = os.getenv("TIENDANUBE_STORE_ID")
ACCESS_TOKEN = os.getenv("TIENDANUBE_ACCESS_TOKEN")
USER_AGENT = os.getenv("TIENDANUBE_USER_AGENT", "BeautyHouseBot (luisenriqvera@gmail.com)")

BASE_URL = "https://api.tiendanube.com/{}/{}".format(API_VERSION, STORE_ID)

HEADERS = {
    "Authentication": "bearer {}".format(ACCESS_TOKEN),
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
}

# Business rules agreed with the store owner
# --------------------------------------------------------------------------
# Low level client
# --------------------------------------------------------------------------

def _get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET with rate limit handling. Read-only."""
    url = "{}{}".format(BASE_URL, endpoint)

    for attempt in range(4):
        response = requests.get(url, headers=HEADERS, params=params, timeout=20)

        if response.status_code == 429:
            wait = int(response.headers.get("Retry-After", 5))
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response.json()

    raise RuntimeError("Rate limited after several retries: {}".format(endpoint))


def _localized(value: Any) -> str:
    """Tiendanube returns some fields as {'es': '...', 'pt': '...'}."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("es", "pt", "en"):
            if value.get(key):
                return value[key]
        values = list(value.values())
        return values[0] if values else ""
    return str(value)


def _describe_variant(variant: Dict[str, Any]) -> str:
    values = variant.get("values") or []
    return " / ".join(_localized(v) for v in values)


# --------------------------------------------------------------------------
# Tools exposed to the LLM
# --------------------------------------------------------------------------

def search_products(query: str, limit: int = 5, include_hidden: bool = False) -> List[Dict[str, Any]]:
    """
    Search products by name. Returns candidates to identify a product.

    This deliberately does NOT return price or stock. The agent must use
    get_stock() for the exact SKU before saying anything about availability or
    price. Hidden products are excluded by default: they are not for sale, so
    the bot must never offer them to a customer.
    """
    products = _get("/products", {"q": query, "per_page": limit})

    results = []
    for product in products:
        if not include_hidden and not product.get("published", False):
            continue
        variants = []
        for variant in product.get("variants", []):
            variants.append({
                "variant_id": variant.get("id"),
                "sku": variant.get("sku") or "",
                "description": _describe_variant(variant),
            })

        results.append({
            "product_id": product.get("id"),
            "name": _localized(product.get("name")),
            "published": product.get("published", False),
            "variants": variants,
        })

    return results


def get_stock(sku: str) -> Dict[str, Any]:
    """
    Return the live availability for a single SKU.

    Four possible states:
        in_stock     -> quantity > 0
        out_of_stock -> quantity == 0
        untracked_stock -> stock is None (Tiendanube does not track it)
    """
    products = _get("/products", {"q": sku, "per_page": 10})

    for product in products:
        for variant in product.get("variants", []):
            if (variant.get("sku") or "").strip().lower() != sku.strip().lower():
                continue

            stock = variant.get("stock")

            if stock is None:
                status = "untracked_stock"
                message = (
                    "No puedo confirmar la disponibilidad automáticamente. "
                    "Lo consultamos con Isa."
                )
            elif stock > 0:
                status = "in_stock"
                message = "Disponible: {} unidades.".format(stock)
            else:
                status = "out_of_stock"
                message = "Sin stock en este momento."

            return {
                "found": True,
                "sku": sku,
                "product_name": _localized(product.get("name")),
                "variant": _describe_variant(variant),
                "status": status,
                "quantity": stock,
                "price": variant.get("price"),
                "message": message,
            }

    return {
        "found": False,
        "sku": sku,
        "message": "No encontré ese código en el catálogo.",
    }


def get_order_status(order_number: str) -> Dict[str, Any]:
    """Look up an order by its number. Replaces the simulated version."""
    orders = _get("/orders", {"q": order_number, "per_page": 5})

    if not orders:
        return {"found": False, "message": "No encontré esa orden."}

    order = orders[0]
    return {
        "found": True,
        "order_number": order.get("number"),
        "payment_status": order.get("payment_status"),
        "shipping_status": order.get("shipping_status"),
        "status": order.get("status"),
        "shipping_method": order.get("shipping_option"),
        "tracking": order.get("shipping_tracking_number"),
        "total": order.get("total"),
    }


# --------------------------------------------------------------------------
# Schemas for the LLM (OpenAI-compatible, works with DeepSeek)
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": (
                "Busca productos por nombre en el catálogo real de la tienda. "
                "Usar cuando la clienta menciona un producto pero no se conoce su SKU. "
                "Solo identifica candidatos y variantes: NO confirma stock ni precio. "
                "Después usá get_stock con el SKU elegido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Nombre o parte del nombre del producto.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Cantidad máxima de resultados. Por defecto 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock",
            "description": (
                "Devuelve la disponibilidad real y actual de un SKU específico. "
                "SIEMPRE usar esta función antes de afirmar que hay o no hay stock. "
                "Nunca inventar ni deducir cantidades."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {
                        "type": "string",
                        "description": "Código SKU exacto de la variante.",
                    },
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Consulta el estado real de un pedido por su número.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_number": {
                        "type": "string",
                        "description": "Número de orden.",
                    },
                },
                "required": ["order_number"],
            },
        },
    },
]

# Registry so the agent loop can dispatch by name
AVAILABLE_TOOLS = {
    "search_products": search_products,
    "get_stock": get_stock,
    "get_order_status": get_order_status,
}


if __name__ == "__main__":
    # Quick smoke test against the real store
    print("Testing connection...\n")

    found = search_products("isabel", limit=3)
    for product in found:
        print("{}  (published: {})".format(product["name"], product["published"]))
        for variant in product["variants"]:
            stock = "unlimited" if variant["stock"] is None else variant["stock"]
            print("    sku={:<24} stock={:<10} {}".format(
                variant["sku"] or "-", stock, variant["description"]
            ))
        print()
