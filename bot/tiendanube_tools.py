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

import re
import time
from html import unescape
from typing import Any, Dict, List, Optional

import requests
from tiendanube_credentials import (
    TiendanubeCredentialError,
    get_tiendanube_configuration,
    invalidate_tiendanube_configuration,
)

API_VERSION = "2025-03"
# Business rules agreed with the store owner
# --------------------------------------------------------------------------
# Low level client
# --------------------------------------------------------------------------

# One pooled session for the whole process instead of a fresh TCP+TLS
# handshake per request. Measured on the real store: ~426ms per call without
# it, ~304ms with it, for identical requests and identical responses. The pool
# is sized for the small parallel fan-outs the callers do (never more than a
# handful of products at a time).
_SESSION = requests.Session()
_SESSION.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8),
)


def _get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """GET with rate limit handling. Read-only."""

    def request_once():
        try:
            configuration = get_tiendanube_configuration()
        except TiendanubeCredentialError as error:
            raise RuntimeError("No puedo consultar Tiendanube ahora.") from error

        url = "https://api.tiendanube.com/{}/{}{}".format(
            API_VERSION, configuration["store_id"], endpoint
        )
        headers = {
            "Authentication": "bearer {}".format(configuration["access_token"]),
            "Content-Type": "application/json",
            "User-Agent": configuration["user_agent"],
        }

        for _attempt in range(4):
            response = _SESSION.get(url, headers=headers, params=params, timeout=20)

            if response.status_code == 429:
                wait = int(response.headers.get("Retry-After", 5))
                time.sleep(wait)
                continue

            # Reported to the caller rather than raised here: a rejected
            # credential is worth one fresh read before giving up.
            if response.status_code in (401, 403):
                return None, response

            response.raise_for_status()
            return response.json(), response

        raise RuntimeError("Rate limited after several retries: {}".format(endpoint))

    payload, response = request_once()
    if payload is not None:
        return payload

    # The credential the cache handed us was rejected. Drop it, read the real
    # one, and try exactly once more -- this is what keeps caching from
    # changing behaviour when the owner re-authorises the app.
    invalidate_tiendanube_configuration()
    payload, response = request_once()
    if payload is not None:
        return payload
    response.raise_for_status()
    return response.json()


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


def _product_description(product: Dict[str, Any], limit: int = 1200) -> str:
    """Convert the store's HTML description into short plain text for the LLM."""
    raw_description = _localized(product.get("description"))
    plain_text = re.sub(r"<[^>]+>", " ", unescape(raw_description))
    plain_text = re.sub(r"\s+", " ", plain_text).strip()
    return plain_text[:limit]


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


def search_available_products(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Find published variants with positively confirmed live stock.

    Unlike search_products(), this is for recommendations. It only returns
    variants whose current Tiendanube stock is greater than zero; stock=None
    is deliberately excluded because it is not confirmed availability.
    """
    products = _get("/products", {"q": query, "per_page": 50})
    # A surprise set cannot responsibly answer a request for a specific effect
    # (natural, cat eye, etc.). It remains discoverable only when the customer
    # actually asks for a surprise/variety product.
    allow_surprise = "sorpresa" in query.lower()
    results = []

    for product in products:
        if not product.get("published", False):
            continue
        product_name = _localized(product.get("name"))
        if not allow_surprise and "sorpresa" in product_name.lower():
            continue

        available_variants = []
        for variant in product.get("variants", []):
            stock = variant.get("stock")
            if stock is None or stock <= 0:
                continue
            available_variants.append({
                "sku": variant.get("sku") or "",
                "description": _describe_variant(variant),
                "quantity": stock,
            })

        if available_variants:
            results.append({
                "product_id": product.get("id"),
                "name": product_name,
                "variants": available_variants,
            })

        if len(results) >= limit:
            break

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
        # A hidden product must never be presented as a customer option.
        if not product.get("published", False):
            continue

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


def get_product_availability(product_id: int) -> Dict[str, Any]:
    """Return live availability for every sellable variant of one product."""
    product = _get("/products/{}".format(product_id))

    if not product.get("published", False):
        return {
            "found": False,
            "product_id": product_id,
            "message": "El producto no está publicado para la venta.",
        }

    variants = []
    for variant in product.get("variants", []):
        stock = variant.get("stock")
        if stock is None:
            status = "untracked_stock"
        elif stock > 0:
            status = "in_stock"
        else:
            status = "out_of_stock"

        variants.append({
            "sku": variant.get("sku") or "",
            "variant": _describe_variant(variant),
            "status": status,
            "quantity": stock,
        })

    return {
        "found": True,
        "product_id": product_id,
        "product_name": _localized(product.get("name")),
        "description": _product_description(product),
        "product_url": product.get("canonical_url") or "",
        "variants": variants,
    }


def get_order_status(order_number: str) -> Dict[str, Any]:
    """Look up an order by its number. Replaces the simulated version."""
    try:
        orders = _get("/orders", {"q": order_number, "per_page": 5})
    except requests.exceptions.HTTPError as error:
        # Tiendanube's search returns 404 (not an empty 200 list) when a
        # query matches nothing -- a customer typo or a real "this order
        # doesn't exist" is a normal outcome here, not an infrastructure
        # failure, and must not be reported as one (that would wrongly skip
        # the deterministic "order not found" handoff and instead look like
        # a live-data outage).
        if error.response is not None and error.response.status_code == 404:
            return {"found": False, "message": "No encontré esa orden."}
        raise

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


def catalog_health_audit(max_pages: int = 30) -> Dict[str, Any]:
    """Read the catalog and flag operational risks without changing it.

    This is deliberately a diagnostic, not a stock reconciliation. A zero
    stock product can be intentional; the audit only helps the owner decide
    what deserves review before exposing it to sales automation.
    """
    if max_pages < 1:
        raise ValueError("max_pages debe ser positivo")

    totals = {
        "products_scanned": 0,
        "variants_scanned": 0,
        "published_without_sku": 0,
        "published_untracked_stock": 0,
        "hidden_with_positive_stock": 0,
        "published_out_of_stock": 0,
        "duplicate_skus": 0,
    }
    samples = {
        "published_without_sku": [],
        "published_untracked_stock": [],
        "hidden_with_positive_stock": [],
        "duplicate_skus": [],
    }
    seen_skus: Dict[str, List[str]] = {}

    for page in range(1, max_pages + 1):
        products = _get("/products", {"page": page, "per_page": 50})
        if not products:
            break
        for product in products:
            totals["products_scanned"] += 1
            product_name = _localized(product.get("name")) or "Producto sin nombre"
            published = bool(product.get("published", False))
            for variant in product.get("variants", []):
                totals["variants_scanned"] += 1
                sku = (variant.get("sku") or "").strip()
                stock = variant.get("stock")
                label = "{} · {}".format(product_name, _describe_variant(variant) or "variante única")

                if published and not sku:
                    totals["published_without_sku"] += 1
                    if len(samples["published_without_sku"]) < 5:
                        samples["published_without_sku"].append(label)
                if published and stock is None:
                    totals["published_untracked_stock"] += 1
                    if len(samples["published_untracked_stock"]) < 5:
                        samples["published_untracked_stock"].append(label)
                if not published and isinstance(stock, (int, float)) and stock > 0:
                    totals["hidden_with_positive_stock"] += 1
                    if len(samples["hidden_with_positive_stock"]) < 5:
                        samples["hidden_with_positive_stock"].append(label)
                if published and isinstance(stock, (int, float)) and stock == 0:
                    totals["published_out_of_stock"] += 1
                if sku:
                    seen_skus.setdefault(sku.upper(), []).append(label)
        if len(products) < 50:
            break

    duplicate_groups = [
        (sku, labels) for sku, labels in seen_skus.items() if len(labels) > 1
    ]
    totals["duplicate_skus"] = len(duplicate_groups)
    for sku, labels in duplicate_groups[:5]:
        samples["duplicate_skus"].append("{}: {}".format(sku, " | ".join(labels[:3])))

    return {"totals": totals, "samples": samples}


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
                "Después usá get_stock o get_product_availability antes de "
                "hablar de disponibilidad."
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
            "name": "search_available_products",
            "description": (
                "Busca productos publicados con stock positivo confirmado en "
                "Tiendanube. Usar para una recomendación genérica (por ejemplo, "
                "'pestañas naturales') antes de decir que no hay alternativas. "
                "Los resultados ya están filtrados por disponibilidad real."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Categoría o nombre simple para buscar.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Máximo de productos. Por defecto 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_availability",
            "description": (
                "Consulta en Tiendanube la disponibilidad real de todas las "
                "variantes publicadas de un producto identificado por product_id. "
                "Usar para comparar candidatos de una recomendación y priorizar "
                "una variante con status in_stock."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "ID interno del producto de Tiendanube.",
                    },
                },
                "required": ["product_id"],
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
    "search_available_products": search_available_products,
    "get_stock": get_stock,
    "get_product_availability": get_product_availability,
    "get_order_status": get_order_status,
}


if __name__ == "__main__":
    # Quick smoke test against the real store
    print("Testing connection...\n")

    found = search_products("isabel", limit=3)
    for product in found:
        print("{}  (published: {})".format(product["name"], product["published"]))
        for variant in product["variants"]:
            print("    sku={:<24} {}".format(
                variant["sku"] or "-", variant["description"]
            ))
        print()
