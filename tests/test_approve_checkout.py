"""Tests for the approve_checkout branch of handle_isa_message -- the single
highest-risk, previously-untested code path in the repo (it is the only
place that can create a real Tiendanube order). Confirmed by audit to have
zero test coverage before this file: not the purchase_review type guard, not
the create_approved_checkout call, not the idempotent-retry behaviour when a
checkout already exists, not the insufficient-stock/CheckoutError path, and
not what happens when the link fails to reach the customer.

Everything here is offline: create_approved_checkout, resolve_pending_action,
send_whatsapp_text and friends are all mocked. No real Tiendanube or
WhatsApp call happens.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


PURCHASE_REVIEW_ACTION = {
    "id": 11,
    "action_type": "purchase_review",
    "customer_phone": "5491111111111",
    "payload": {
        "sale_draft": {
            "items_status": "2 × SHOOW TOOLS - ISABEL I",
            "selected_sku": "ISABEL-1",
            "customer_name": "Ana Pérez",
            "customer_email": "ana@example.com",
        },
    },
}
CHECKOUT = {"id": 999, "checkout_url": "https://tiendanube.example/checkout/999"}


# handle_isa_message scans open cases before routing a button. That scan is
# a Supabase read, and a unit test must not depend on the developer having
# a populated .env -- these classes are about the checkout decision, not
# about what else happens to be pending.
@patch.object(app, "list_pending_actions", new=lambda **kwargs: [])
class ApproveCheckoutTypeGuardTests(unittest.TestCase):
    @patch.object(app, "create_approved_checkout")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "_pending_action_by_id", return_value=None)
    def test_missing_pending_action_never_calls_checkout(self, get_pending, send_message, create_checkout):
        app.handle_isa_message("", button_reply_id="approve_checkout:11")

        create_checkout.assert_not_called()
        self.assertIn("ya no está disponible", send_message.call_args.args[1])

    @patch.object(app, "create_approved_checkout")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(
        app, "_pending_action_by_id",
        return_value={"id": 12, "action_type": "human_handoff", "payload": {}},
    )
    def test_non_purchase_review_action_never_calls_checkout(self, get_pending, send_message, create_checkout):
        app.handle_isa_message("", button_reply_id="approve_checkout:12")

        create_checkout.assert_not_called()
        self.assertIn("no es una compra para aprobar", send_message.call_args.args[1])


@patch.object(app, "list_pending_actions", new=lambda **kwargs: [])
class ApproveCheckoutHappyPathTests(unittest.TestCase):
    """Approval revalidates the SAME identity and sends the real Tiendanube
    product link. Fred no longer builds a checkout of his own -- cart, data,
    shipping and payment all happen in the store."""

    PENDING = {
        "id": 7, "conversation_id": 11, "action_type": "purchase_review",
        "customer_phone": "5491111111111",
        "payload": {"customer_phone": "5491111111111", "sale_draft": {
            "items_status": "2 × AOA STUDIO - PEGA DE PESTAÑAS COREANA",
            "selected_sku": "3D24A",
        }},
    }

    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 11})
    @patch.object(app, "_product_url_for_sku", return_value="https://beautyhousemakeup.com/productos/aoa/")
    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 19, "sku": "3D24A",
    })
    @patch.object(app, "_pending_action_by_id")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_approval_revalidates_and_sends_the_real_product_link(
        self, send_message, by_id, get_stock, product_url, resolve, record, set_state, count,
    ):
        by_id.return_value = self.PENDING
        with patch.object(app, "create_approved_checkout") as create_checkout:
            app.handle_isa_message("", button_reply_id="approve_checkout:7")

        # The same SKU the card showed is revalidated, and no custom checkout
        # is created any more.
        get_stock.assert_called_with("3D24A")
        create_checkout.assert_not_called()
        to_customer = [c.args[1] for c in send_message.call_args_list if c.args[0] == "5491111111111"]
        self.assertTrue(any("https://beautyhousemakeup.com/productos/aoa/" in m for m in to_customer))
        resolve.assert_called_once_with(7, "approved")

    @patch.object(app, "_product_url_for_sku", return_value="")
    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 19, "sku": "3D24A",
    })
    @patch.object(app, "_pending_action_by_id")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_without_a_public_link_nothing_is_sent_and_the_case_stays_open(
        self, send_message, by_id, get_stock, product_url,
    ):
        by_id.return_value = self.PENDING
        with patch.object(app, "resolve_pending_action") as resolve:
            app.handle_isa_message("", button_reply_id="approve_checkout:7")
        resolve.assert_not_called()
        to_customer = [c.args[1] for c in send_message.call_args_list if c.args[0] == "5491111111111"]
        self.assertEqual(to_customer, [])


@patch.object(app, "list_pending_actions", new=lambda **kwargs: [])
class ApproveCheckoutFailureTests(unittest.TestCase):
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "save_pending_action_checkout")
    @patch.object(app, "create_approved_checkout", side_effect=app.CheckoutError("La variante ya no tiene stock suficiente."))
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "_pending_action_by_id", return_value=PURCHASE_REVIEW_ACTION)
    def test_insufficient_stock_creates_nothing_and_leaves_the_case_open(
        self, get_pending, send_message, create_checkout, save_checkout, resolve_action,
    ):
        app.handle_isa_message("", button_reply_id="approve_checkout:11")

        save_checkout.assert_not_called()
        resolve_action.assert_not_called()
        # Only Isa hears about the failure -- the customer is never told
        # anything (no half-finished checkout, no confusing message).
        customer_messages = [c.args for c in send_message.call_args_list if c.args[0] == "5491111111111"]
        self.assertEqual(customer_messages, [])
        isa_messages = [c.args[1] for c in send_message.call_args_list if c.args[0] == app.ISA_WHATSAPP_NUMBER]
        self.assertTrue(any("el pendiente sigue abierto" in message.lower() for message in isa_messages))

    @patch.object(app, "_product_url_for_sku", return_value="https://beautyhousemakeup.com/p/isabel/")
    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 40, "sku": "ISABEL-1",
    })
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "_pending_action_by_id", return_value=PURCHASE_REVIEW_ACTION)
    def test_delivery_failure_never_resolves_the_case(
        self, get_pending, resolve_action, get_stock, product_url,
    ):
        # False to the customer (delivery failed), True to Isa (the warning).
        with patch.object(app, "send_whatsapp_text", side_effect=[False, True]):
            app.handle_isa_message("", button_reply_id="approve_checkout:11")
        resolve_action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
