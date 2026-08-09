"""Deterministic checks for Fred's pre-approval sales flow.

These tests never call Meta, DeepSeek, Tiendanube or Supabase. They protect
the small decisions that must always behave the same way, independently of
how a language model phrases an answer.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

# Importing app creates a Gemini client, but these tests never call it.
os.environ.setdefault("GEMINI_API_KEY", "test-key")
import app  # noqa: E402
import agent  # noqa: E402


def _intake(status="confirmation"):
    return {
        "status": status,
        "product_request": "Isabel I chocolate",
        "selected_sku": "ISABEL-CHOC",
        "selected_variant": "8/8/10/12 mm",
        "unit_price": "30000.00",
        "quantity": 4,
        "fulfillment": "shipping",
        "customer_name": "Luis Vera",
        "customer_email": "luis@example.com",
    }


class SalesFlowTests(unittest.TestCase):
    def test_compact_customer_details_excludes_fulfillment_word(self):
        self.assertEqual(
            app._extract_customer_details("envío, Luis Vera, luis@example.com"),
            ("Luis Vera", "luis@example.com"),
        )

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "mark_sales_intake_ready")
    def test_confirmo_creates_pending_review_not_a_new_form(
        self, mark_ready, queue_for_isa, record_message, send_message
    ):
        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "confirmo",
            _intake(),
            [{"role": "user", "content": "quiero 4 Isabel I"}],
        )

        self.assertTrue(handled)
        mark_ready.assert_called_once_with(11)
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "purchase_review")
        self.assertIn("ya se lo pasé a Isa", send_message.call_args.args[1])
        record_message.assert_called_once()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "get_active_sales_intake", return_value=_intake())
    @patch.object(app, "set_sales_intake_customer")
    @patch.object(app, "set_sales_intake_fulfillment")
    def test_customer_details_complete_the_real_form(
        self, set_fulfillment, set_customer, get_active, record_message, send_message
    ):
        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "envío, Luis Vera, luis@example.com",
            _intake(status="fulfillment"),
            [],
        )

        self.assertTrue(handled)
        set_fulfillment.assert_called_once_with(11, "shipping")
        set_customer.assert_called_once_with(11, "Luis Vera", "luis@example.com")
        self.assertIn("Subtotal de productos: $120.000", send_message.call_args.args[1])
        record_message.assert_called_once()
        get_active.assert_called_once_with(11)

    @patch.object(app, "get_stock")
    def test_1000_purchase_guard_variations(self, get_stock):
        """500 purchase variations + 500 non-purchase variations.

        This is deliberately a deterministic stress test of the guard, not a
        claim that a generative model can be certified by 1,000 fake chats.
        """
        get_stock.side_effect = lambda sku: {
            "status": "in_stock",
            "sku": sku,
            "product_name": "Modelo de prueba",
            "variant": "Única",
            "price": "1000.00",
        }

        for index in range(500):
            result = {"tool_calls": [{"name": "get_stock", "arguments": {"sku": "SKU-{}".format(index)}}]}
            candidate = app._verified_purchase_candidate_from_tool_calls(
                "quiero comprar el modelo {}".format(index), result
            )
            self.assertEqual(candidate["sku"], "SKU-{}".format(index))

            no_purchase = app._verified_purchase_candidate_from_tool_calls(
                "quiero ver el modelo {}".format(index), result
            )
            self.assertEqual(no_purchase, {})

        self.assertEqual(get_stock.call_count, 500)

    @patch.object(app, "get_stock")
    def test_ambiguous_multiple_skus_never_starts_a_sale(self, get_stock):
        candidate = app._verified_purchase_candidate_from_tool_calls(
            "quiero comprar pestañas naturales",
            {
                "tool_calls": [
                    {"name": "get_stock", "arguments": {"sku": "A"}},
                    {"name": "get_stock", "arguments": {"sku": "B"}},
                ]
            },
        )
        self.assertEqual(candidate, {})
        get_stock.assert_not_called()


class AgentOutputSafetyTests(unittest.TestCase):
    def test_unverified_link_is_removed(self):
        self.assertEqual(
            agent._remove_unverified_urls("Mirá https://inventado.test/producto", []),
            "Mirá",
        )

    def test_catalog_markdown_is_flattened_for_whatsapp(self):
        self.assertEqual(
            agent._plain_whatsapp_text("**Isabel I**"),
            "Isabel I",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
