"""Verified, idempotent Tiendanube webhook handling for Fred orders."""

import hashlib
import hmac
import os
from typing import Any, Dict

import requests

from tiendanube_credentials import (
    TiendanubeCredentialError,
    get_tiendanube_configuration,
)


API_VERSION = "2025-03"


def webhook_signature_is_valid(raw_body: bytes, signature: str) -> bool:
    """Validate Tiendanube's HMAC header with the app's client secret."""
    secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def fetch_paid_order(order_id: str) -> Dict[str, Any]:
    """Read an order after a verified order/paid notification; never writes."""
    try:
        configuration = get_tiendanube_configuration()
    except TiendanubeCredentialError as error:
        raise RuntimeError("No se pudo leer Tiendanube.") from error

    url = "https://api.tiendanube.com/{}/{}/orders/{}".format(
        API_VERSION, configuration["store_id"], order_id
    )
    try:
        response = requests.get(
            url,
            headers={
                "Authentication": "bearer {}".format(configuration["access_token"]),
                "Content-Type": "application/json",
                "User-Agent": configuration["user_agent"],
            },
            timeout=15,
        )
    except requests.RequestException as error:
        raise RuntimeError("No se pudo consultar la orden en Tiendanube.") from error
    if not response.ok:
        raise RuntimeError("Tiendanube no devolvió la orden pagada.")
    order = response.json()
    if str(order.get("id")) != str(order_id) or order.get("payment_status") != "paid":
        raise RuntimeError("La orden todavía no figura como pagada.")
    return order
