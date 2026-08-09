"""Safe checkout-link creation for approved WhatsApp sales.

The customer completes the Tiendanube checkout.  Fred never creates a paid
order and never changes stock itself: Tiendanube owns both the order and
inventory lifecycle.
"""

import os
from typing import Any, Dict

import requests


API_VERSION = "2025-03"


class CheckoutError(RuntimeError):
    """A checkout could not be created without risking an incorrect sale."""


def checkout_enabled() -> bool:
    """Production writes require an explicit Railway switch."""
    return os.getenv("TIENDANUBE_CHECKOUT_MODE", "disabled").lower() == "production"


def _configuration() -> Dict[str, str]:
    if not checkout_enabled():
        raise CheckoutError(
            "El checkout real está apagado. No se creó ningún link ni pedido."
        )

    store_id = os.getenv("TIENDANUBE_STORE_ID", "").strip()
    access_token = os.getenv("TIENDANUBE_ACCESS_TOKEN", "").strip()
    user_agent = os.getenv(
        "TIENDANUBE_USER_AGENT", "BeautyHouseBot (support@example.com)"
    ).strip()
    if not store_id or not access_token:
        raise CheckoutError("Falta la configuración de Tiendanube en Railway.")
    return {
        "store_id": store_id,
        "access_token": access_token,
        "user_agent": user_agent,
    }


def _headers(configuration: Dict[str, str]) -> Dict[str, str]:
    return {
        "Authentication": "bearer {}".format(configuration["access_token"]),
        "Content-Type": "application/json",
        "User-Agent": configuration["user_agent"],
    }


def _request(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Any = None,
    params: Dict[str, Any] = None,
) -> Any:
    try:
        response = requests.request(
            method, url, headers=headers, json=payload, params=params, timeout=20
        )
    except requests.RequestException as error:
        raise CheckoutError("No se pudo conectar con Tiendanube.") from error
    if not response.ok:
        raise CheckoutError(
            "Tiendanube respondió HTTP {}: {}".format(
                response.status_code, response.text[:300]
            )
        )
    return response.json()


def _localized(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("es") or value.get("pt") or value.get("en") or "")
    return ""


def _find_sellable_variant(
    configuration: Dict[str, str], sku: str, quantity: int
) -> Dict[str, Any]:
    url = "https://api.tiendanube.com/{}/{}/products".format(
        API_VERSION, configuration["store_id"]
    )
    products = _request(
        "GET", url, _headers(configuration), params={"q": sku, "per_page": 10}
    )
    for product in products:
        if not product.get("published", False):
            continue
        for variant in product.get("variants", []):
            if (variant.get("sku") or "").strip().upper() != sku.strip().upper():
                continue
            stock = variant.get("stock")
            if stock is None:
                raise CheckoutError(
                    "La variante seleccionada es por encargo; no se crea como venta normal."
                )
            if int(stock) < quantity:
                raise CheckoutError(
                    "La variante ya no tiene stock suficiente para esa cantidad."
                )
            return {
                "variant_id": int(variant["id"]),
                "product_name": _localized(product.get("name")),
                "price": variant.get("price"),
            }
    raise CheckoutError("No encontré una variante publicada y vendible con ese SKU.")


def _split_customer_name(full_name: str) -> tuple[str, str]:
    parts = [part for part in full_name.strip().split() if part]
    if len(parts) < 2:
        raise CheckoutError("Falta nombre y apellido de la clienta.")
    return parts[0], " ".join(parts[1:])


def create_approved_checkout(
    *,
    sku: str,
    quantity: int,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
) -> Dict[str, Any]:
    """Create an unpaid Tiendanube draft order and return its checkout URL.

    The checkout is intentionally created only after Isa approval.  Address,
    shipping method and payment are chosen by the customer in Tiendanube.
    """
    if quantity < 1:
        raise CheckoutError("La cantidad debe ser al menos 1.")
    configuration = _configuration()
    variant = _find_sellable_variant(configuration, sku, quantity)
    first_name, last_name = _split_customer_name(customer_name)
    url = "https://api.tiendanube.com/{}/{}/draft_orders".format(
        API_VERSION, configuration["store_id"]
    )
    payload = {
        "contact_name": first_name,
        "contact_lastname": last_name,
        "contact_email": customer_email,
        "contact_phone": customer_phone,
        "payment_status": "unpaid",
        "sale_channel": "Fred WhatsApp",
        "products": [{"variant_id": variant["variant_id"], "quantity": quantity}],
    }
    draft_order = _request("POST", url, _headers(configuration), payload)
    checkout_url = draft_order.get("checkout_url")
    if not checkout_url:
        raise CheckoutError("Tiendanube creó el borrador pero no devolvió un link de checkout.")
    return {
        "id": draft_order.get("id"),
        "checkout_url": checkout_url,
        "product_name": variant["product_name"],
        "quantity": quantity,
        "price": variant["price"],
    }
