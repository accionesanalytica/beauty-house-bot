"""No-network checks for the approved real-checkout adapter."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import tiendanube_checkout as checkout  # noqa: E402


class CheckoutTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "TIENDANUBE_CHECKOUT_MODE": "production",
            "TIENDANUBE_STORE_ID": "123",
            "TIENDANUBE_ACCESS_TOKEN": "secret",
            "TIENDANUBE_USER_AGENT": "Fred test",
        },
        clear=False,
    )
    @patch.object(checkout.requests, "request")
    def test_creates_unpaid_checkout_after_live_stock_recheck(self, request):
        products = Mock(ok=True)
        products.json.return_value = [
            {
                "published": True,
                "name": {"es": "Isabel I"},
                "variants": [{"id": 99, "sku": "ISABEL-CHOC", "stock": 5, "price": "30000.00"}],
            }
        ]
        draft = Mock(ok=True)
        draft.json.return_value = {"id": 777, "checkout_url": "https://checkout.test/777"}
        request.side_effect = [products, draft]

        result = checkout.create_approved_checkout(
            sku="ISABEL-CHOC",
            quantity=4,
            customer_name="Luis Vera",
            customer_email="luis@example.com",
            customer_phone="5491111111111",
        )

        self.assertEqual(result["id"], 777)
        self.assertEqual(request.call_count, 2)
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["payment_status"], "unpaid")
        self.assertEqual(payload["products"], [{"variant_id": 99, "quantity": 4}])

    @patch.dict(os.environ, {"TIENDANUBE_CHECKOUT_MODE": "disabled"}, clear=False)
    def test_cannot_create_checkout_until_explicitly_enabled(self):
        with self.assertRaisesRegex(checkout.CheckoutError, "apagado"):
            checkout.create_approved_checkout(
                sku="ISABEL-CHOC",
                quantity=1,
                customer_name="Luis Vera",
                customer_email="luis@example.com",
                customer_phone="5491111111111",
            )

    @patch.dict(
        os.environ,
        {
            "TIENDANUBE_CHECKOUT_MODE": "production",
            "TIENDANUBE_STORE_ID": "123",
            "TIENDANUBE_ACCESS_TOKEN": "secret",
        },
        clear=False,
    )
    @patch.object(checkout.requests, "request")
    def test_insufficient_stock_prevents_checkout(self, request):
        products = Mock(ok=True)
        products.json.return_value = [
            {
                "published": True,
                "name": "Isabel I",
                "variants": [{"id": 99, "sku": "ISABEL-CHOC", "stock": 3}],
            }
        ]
        request.return_value = products
        with self.assertRaisesRegex(checkout.CheckoutError, "stock suficiente"):
            checkout.create_approved_checkout(
                sku="ISABEL-CHOC",
                quantity=4,
                customer_name="Luis Vera",
                customer_email="luis@example.com",
                customer_phone="5491111111111",
            )
        self.assertEqual(request.call_count, 1)
