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

    def test_labeled_customer_details_exclude_intro_words(self):
        self.assertEqual(
            app._extract_customer_details(
                "genial te dejo los datos:\nEntrega: envío\nNombre y apellido: Luis Vera\nEmail: luis@example.com"
            ),
            ("Luis Vera", "luis@example.com"),
        )

    def test_unlabeled_intro_words_do_not_become_customer_name(self):
        self.assertEqual(
            app._extract_customer_details(
                "genial te dejo los datos: envio, Luis Vera, luis@example.com"
            ),
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


class IsaInternalSaleFlowTests(unittest.TestCase):
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "wait_for_isa_response", return_value=True)
    @patch.object(
        app,
        "_pending_action_by_id",
        return_value={"id": 31, "action_type": "bot_fallback"},
    )
    def test_isa_can_reply_to_fred_with_a_reviewed_answer(
        self, get_pending, wait_for_reply, send_message
    ):
        app.handle_isa_message("", button_reply_id="reply_to_fred:31")
        wait_for_reply.assert_called_once_with(31)
        self.assertIn("Escribime la respuesta", send_message.call_args.args[1])

    @patch.object(app, "send_isa_sale_type_menu", return_value=True)
    @patch.object(app, "start_isa_sale_session")
    @patch.object(app, "get_isa_sale_session", return_value=None)
    def test_natural_external_sale_request_opens_category_menu(
        self, get_session, start_session, send_menu
    ):
        app.handle_isa_message("Vendí unos productos por Instagram, ¿me armás el link?")
        start_session.assert_called_once_with(app.ISA_WHATSAPP_NUMBER)
        send_menu.assert_called_once()
        get_session.assert_called_once_with(app.ISA_WHATSAPP_NUMBER)

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "set_isa_sale_session_type")
    def test_isa_can_choose_encargo_without_writing_a_command(self, set_type, send_message):
        handled = app._handle_isa_sale_session("Encargo", "sale_type:encargo")
        self.assertTrue(handled)
        set_type.assert_called_once_with(app.ISA_WHATSAPP_NUMBER, "encargo")
        self.assertIn("Encargo", send_message.call_args.args[1])

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "add_isa_sale_session_details")
    @patch.object(
        app,
        "get_isa_sale_session",
        return_value={"status": "collect_details", "sale_type": "venta_mayorista", "details": None},
    )
    def test_selected_type_keeps_details_for_review(self, get_session, add_details, send_message):
        handled = app._handle_isa_sale_session(
            "Isabel I chocolate x 12, Cliente Ejemplo, cliente@example.com", ""
        )
        self.assertTrue(handled)
        add_details.assert_called_once()
        self.assertIn("Venta mayorista", send_message.call_args.args[1])

    @patch.object(
        app,
        "get_isa_sale_session",
        return_value={"status": "review", "sale_type": "encargo", "details": "algo"},
    )
    def test_customer_approval_button_is_not_captured_by_internal_draft(self, get_session):
        self.assertFalse(app._handle_isa_sale_session("Tomar caso", "approve:17"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
