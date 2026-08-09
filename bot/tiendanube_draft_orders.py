"""Demo-only Tiendanube draft-order operations.

This module is intentionally isolated from the live catalog integration. It
only accepts the dedicated Fred demo store, so it cannot create orders in
Beauty House production by configuration mistake.
"""

import json
import os
from typing import Any, Dict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEMO_STORE_ID = "8068892"
API_BASE_URL = "https://api.tiendanube.com/v1/{store_id}"


class DraftOrderDemoError(RuntimeError):
    """Raised when a demo order cannot be created safely."""


def demo_draft_orders_enabled() -> bool:
    """Return true only for the explicit, dedicated demo mode."""
    return os.getenv("TIENDANUBE_DRAFT_ORDERS_MODE", "disabled").lower() == "demo"


def _configuration(require_enabled: bool = True) -> Dict[str, str]:
    if require_enabled and not demo_draft_orders_enabled():
        raise DraftOrderDemoError(
            "El modo de órdenes demo está apagado. No se creó ninguna orden."
        )

    store_id = os.getenv("TIENDANUBE_DEMO_STORE_ID", "").strip()
    access_token = os.getenv("TIENDANUBE_DEMO_ACCESS_TOKEN", "").strip()
    user_agent = os.getenv(
        "TIENDANUBE_USER_AGENT", "BeautyHouseBot (support@example.com)"
    ).strip()

    if store_id != DEMO_STORE_ID:
        raise DraftOrderDemoError(
            "Esta función solo permite la tienda demo {}.".format(DEMO_STORE_ID)
        )
    if not access_token:
        raise DraftOrderDemoError("Falta TIENDANUBE_DEMO_ACCESS_TOKEN.")

    return {
        "store_id": store_id,
        "access_token": access_token,
        "user_agent": user_agent,
    }


def _headers(configuration: Dict[str, str]) -> Dict[str, str]:
    return {
        "Authorization": "Bearer {}".format(configuration["access_token"]),
        "Content-Type": "application/json",
        "User-Agent": configuration["user_agent"],
    }


def _request_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Any = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise DraftOrderDemoError(
            "Tiendanube respondió HTTP {}: {}".format(error.code, detail)
        ) from error
    except URLError as error:
        raise DraftOrderDemoError(
            "No se pudo conectar con Tiendanube: {}".format(error.reason)
        ) from error


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("es") or value.get("pt") or value.get("en") or "")
    return ""


def _find_published_variant(
    configuration: Dict[str, str], sku: str
) -> Dict[str, Any]:
    url = "{}/products?{}".format(
        API_BASE_URL.format(**configuration),
        urlencode({"q": sku, "per_page": 10}),
    )
    products = _request_json("GET", url, _headers(configuration))

    for product in products:
        if not product.get("published", False):
            continue
        for variant in product.get("variants", []):
            if (variant.get("sku") or "").strip().upper() != sku.upper():
                continue
            stock = variant.get("stock")
            if stock is None or int(stock) < 1:
                raise DraftOrderDemoError(
                    "La variante {} no tiene stock demo disponible.".format(sku)
                )
            return {
                "product_name": _localized(product.get("name")),
                "variant_id": int(variant["id"]),
                "stock": stock,
                "price": variant.get("price"),
            }

    raise DraftOrderDemoError(
        "No encontré una variante publicada con SKU {} en la demo.".format(sku)
    )


def create_demo_draft_order(
    sku: str,
    quantity: int,
    require_enabled: bool = True,
) -> Dict[str, Any]:
    """Create one unpaid draft in Fred's dedicated demo store.

    The fixed contact information is test data. This must never be reused for
    a production order flow.
    """
    if quantity < 1:
        raise DraftOrderDemoError("La cantidad debe ser al menos 1.")

    configuration = _configuration(require_enabled=require_enabled)
    variant = _find_published_variant(configuration, sku.strip())
    url = "{}/draft_orders".format(API_BASE_URL.format(**configuration))
    payload = {
        "contact_name": "Prueba",
        "contact_lastname": "Fred",
        "contact_email": "prueba.fred.demo@example.com",
        "payment_status": "unpaid",
        "note": "PRUEBA DEMO - no es una venta real",
        "products": [{"variant_id": variant["variant_id"], "quantity": quantity}],
    }
    draft_order = _request_json("POST", url, _headers(configuration), payload)
    return {
        "id": draft_order.get("id"),
        "checkout_url": draft_order.get("checkout_url"),
        "payment_status": draft_order.get("payment_status"),
        "product_name": variant["product_name"],
        "sku": sku.strip().upper(),
        "quantity": quantity,
        "price": variant["price"],
    }
