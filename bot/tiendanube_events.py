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


def register_order_paid_webhook(callback_url: str) -> Dict[str, Any]:
    """Register the single owner-approved paid-order callback once."""
    if not callback_url.startswith("https://"):
        raise ValueError("La URL de pagos debe usar HTTPS.")
    try:
        configuration = get_tiendanube_configuration()
    except TiendanubeCredentialError as error:
        raise RuntimeError("No se pudo leer Tiendanube.") from error

    base_url = "https://api.tiendanube.com/{}/{}".format(
        API_VERSION, configuration["store_id"]
    )
    headers = {
        "Authentication": "bearer {}".format(configuration["access_token"]),
        "Content-Type": "application/json",
        "User-Agent": configuration["user_agent"],
    }
    try:
        existing = requests.get(
            base_url + "/webhooks",
            params={"event": "order/paid", "url": callback_url},
            headers=headers,
            timeout=15,
        )
        existing.raise_for_status()
        for webhook in existing.json():
            if webhook.get("event") == "order/paid" and webhook.get("url") == callback_url:
                return {"created": False, "id": webhook.get("id")}

        created = requests.post(
            base_url + "/webhooks",
            json={"event": "order/paid", "url": callback_url},
            headers=headers,
            timeout=15,
        )
        created.raise_for_status()
        webhook = created.json()
        return {"created": True, "id": webhook.get("id")}
    except requests.RequestException as error:
        raise RuntimeError("Tiendanube no pudo registrar el aviso de pago.") from error
