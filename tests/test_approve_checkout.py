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


class ApproveCheckoutHappyPathTests(unittest.TestCase):
    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 7})
    @patch.object(app, "save_pending_action_checkout")
    @patch.object(app, "create_approved_checkout", return_value=CHECKOUT)
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "_pending_action_by_id", return_value=PURCHASE_REVIEW_ACTION)
    def test_creates_checkout_sends_link_and_resolves_the_pending_case(
        self, get_pending, send_message, create_checkout, save_checkout,
        resolve_action, set_state, record_message, pending_count,
    ):
        app.handle_isa_message("", button_reply_id="approve_checkout:11")

        create_checkout.assert_called_once_with(
            sku="ISABEL-1", quantity=2,
            customer_name="Ana Pérez", customer_email="ana@example.com",
            customer_phone="5491111111111",
        )
        save_checkout.assert_called_once_with(11, CHECKOUT)
        customer_messages = [c.args for c in send_message.call_args_list if c.args[0] == "5491111111111"]
        self.assertEqual(len(customer_messages), 1)
        self.assertIn(CHECKOUT["checkout_url"], customer_messages[0][1])
        resolve_action.assert_called_once_with(11, "approved")
        set_state.assert_called_once_with(7, "BOT")
        record_message.assert_called_once_with(7, customer_messages[0][1])

    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 7})
    @patch.object(app, "save_pending_action_checkout")
    @patch.object(app, "create_approved_checkout")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "_pending_action_by_id")
    def test_retry_with_an_existing_checkout_never_creates_a_second_order(
        self, get_pending, send_message, create_checkout, save_checkout,
        resolve_action, set_state, record_message, pending_count,
    ):
        # A retried "Aprobar compra" tap (e.g. after a delivery failure) must
        # reuse the already-created checkout, never place a second real
        # Tiendanube order for the same approval.
        action_with_checkout = {
            **PURCHASE_REVIEW_ACTION,
            "payload": {**PURCHASE_REVIEW_ACTION["payload"], "checkout": CHECKOUT},
        }
        get_pending.return_value = action_with_checkout

        app.handle_isa_message("", button_reply_id="approve_checkout:11")

        create_checkout.assert_not_called()
        save_checkout.assert_not_called()
        customer_messages = [c.args for c in send_message.call_args_list if c.args[0] == "5491111111111"]
        self.assertIn(CHECKOUT["checkout_url"], customer_messages[0][1])
        resolve_action.assert_called_once_with(11, "approved")


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

    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "save_pending_action_checkout")
    @patch.object(app, "create_approved_checkout", return_value=CHECKOUT)
    @patch.object(app, "_pending_action_by_id", return_value=PURCHASE_REVIEW_ACTION)
    def test_checkout_created_but_customer_delivery_fails_never_resolves_the_case(
        self, get_pending, create_checkout, save_checkout, resolve_action,
        set_state, record_message, pending_count,
    ):
        # send_whatsapp_text: False to the customer (delivery failure), True
        # to Isa (the follow-up warning).
        with patch.object(app, "send_whatsapp_text", side_effect=[False, True]) as send_message:
            app.handle_isa_message("", button_reply_id="approve_checkout:11")

        # The checkout itself was already created and persisted -- retrying
        # "Aprobar compra" must reuse it, never place a second order.
        create_checkout.assert_called_once()
        save_checkout.assert_called_once_with(11, CHECKOUT)
        resolve_action.assert_not_called()
        set_state.assert_not_called()
        self.assertIn("Aprobar compra", send_message.call_args_list[-1].args[1])


if __name__ == "__main__":
    unittest.main()
