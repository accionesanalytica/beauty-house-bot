"""The Isa loop: a purchase review is a decision (approve/reject/ask), a
consultation is a human thread (reply-and-close / reply-and-keep-open /
close). Both reach the customer only through Fred, and Fred never claims a
case was handed to Isa when the notification did not actually go out.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
import app  # noqa: E402


def _action(action_type="purchase_review", **overrides):
    action = {
        "id": 42,
        "action_type": action_type,
        "summary": "La clienta confirmó una ficha de venta completa.",
        "payload": {"customer_phone": "5491111111111"},
        "customer_phone": "5491111111111",
        "conversation_id": 7,
    }
    action.update(overrides)
    return action


class HonestIsaNotificationTests(unittest.TestCase):
    """Fred may only say "se lo pasé a Isa" when the message really left."""

    def test_confirmation_text_reflects_a_successful_notification(self):
        self.assertIn("se lo pasé a Isa", app._isa_handoff_confirmation(True))

    def test_confirmation_text_never_claims_delivery_that_failed(self):
        text = app._isa_handoff_confirmation(False)
        self.assertNotIn("se lo pasé", text)
        self.assertIn("quedó registrada", text)

    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=0)
    def test_queue_reports_false_when_every_channel_fails(
        self, count, set_state, create_action,
    ):
        with patch.object(app, "send_isa_pending_buttons", return_value=False), patch.object(
            app, "send_isa_pending_notification", return_value=False,
        ):
            notified = app._queue_for_isa(
                7, "5491111111111", "purchase_review", "resumen", "mensaje",
            )
        # The case is still created -- it is registered, never lost.
        create_action.assert_called_once()
        self.assertFalse(notified)

    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=0)
    def test_queue_reports_true_when_the_card_reaches_isa(
        self, count, set_state, create_action,
    ):
        with patch.object(app, "send_isa_pending_buttons", return_value=True):
            self.assertTrue(app._queue_for_isa(
                7, "5491111111111", "human_handoff", "resumen", "mensaje",
            ))

    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=3)
    def test_template_is_not_resent_when_isa_already_has_a_queue(
        self, count, set_state, create_action,
    ):
        with patch.object(app, "send_isa_pending_buttons", return_value=False), patch.object(
            app, "send_isa_pending_notification",
        ) as template:
            app._queue_for_isa(7, "5491111111111", "human_handoff", "resumen", "mensaje")
        template.assert_not_called()


class RejectPurchaseTests(unittest.TestCase):
    @patch.object(app, "send_next_pending_to_isa")
    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "reset_fred_core_checkout")
    @patch.object(app, "cancel_sales_intake")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 7})
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_reason_reaches_the_customer_and_no_checkout_is_created(
        self, send_message, resolve, record, set_state, cancel_intake, reset_checkout,
        count, next_pending,
    ):
        with patch.object(app, "create_approved_checkout") as create_checkout:
            app._reject_purchase_with_reason(
                _action(), "No tenemos suficiente stock para esa cantidad.",
            )
        create_checkout.assert_not_called()
        customer_text = send_message.call_args_list[0].args[1]
        self.assertIn("No tenemos suficiente stock", customer_text)
        # And a real way forward, not a dead end.
        self.assertIn("otra cantidad", customer_text)
        resolve.assert_called_once_with(42, "rejected")
        cancel_intake.assert_called_once_with(7)

    @patch.object(app, "send_whatsapp_text", return_value=False)
    @patch.object(app, "resolve_pending_action")
    def test_a_failed_send_keeps_the_case_open(self, resolve, send_message):
        app._reject_purchase_with_reason(_action(), "sin stock")
        resolve.assert_not_called()


class AskCustomerTests(unittest.TestCase):
    @patch.object(app, "set_isa_awaiting")
    @patch.object(app, "clear_isa_awaiting")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_question_is_relayed_and_the_review_stays_open(
        self, send_message, resolve, record, set_state, clear_awaiting, set_awaiting,
    ):
        app._ask_customer_for_purchase(_action(), "¿Podés retirar mañana por la tarde?")
        customer_text = send_message.call_args_list[0].args[1]
        self.assertIn("¿Podés retirar mañana por la tarde?", customer_text)
        # The purchase review must NOT be resolved -- Isa still has to decide.
        resolve.assert_not_called()
        set_awaiting.assert_called_once_with(42, "customer_answer")


class CustomerAnswerRoutingTests(unittest.TestCase):
    @patch.object(app, "clear_isa_awaiting")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "list_pending_actions")
    def test_answer_to_an_open_question_goes_back_to_isa(
        self, list_actions, send_message, clear_awaiting,
    ):
        list_actions.return_value = [_action(
            payload={"customer_phone": "5491111111111", "awaiting_isa_kind": "customer_answer"},
        )]
        app._forward_customer_answer_to_isa(7, "5491111111111", "Sí, puedo.")
        relayed = send_message.call_args.args[1]
        self.assertIn("Sí, puedo.", relayed)
        self.assertIn("compra en revisión", relayed)
        clear_awaiting.assert_called_once_with(42)

    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "list_pending_actions", return_value=[])
    def test_nothing_is_relayed_when_no_case_is_waiting(self, list_actions, send_message):
        app._forward_customer_answer_to_isa(7, "5491111111111", "¿cuánto salen las Taylor?")
        send_message.assert_not_called()

    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "list_pending_actions")
    def test_another_conversation_open_case_is_never_touched(self, list_actions, send_message):
        list_actions.return_value = [_action(
            conversation_id=99,
            payload={"customer_phone": "5490000000000", "awaiting_isa_kind": "customer_answer"},
        )]
        app._forward_customer_answer_to_isa(7, "5491111111111", "Sí, puedo.")
        send_message.assert_not_called()


class IsaLegendTests(unittest.TestCase):
    def test_isa_can_ask_what_each_option_does(self):
        for text in (
            "AYUDA", "ayuda", "Ayuda!", "¿qué hace cada opción?",
            "no entiendo las opciones", "explicame las opciones",
            "¿qué pasa si apruebo?", "que pasa si rechazo",
            "¿qué pasa si le doy a pedir algo?", "para qué sirve cada botón",
            "dudas", "¿qué hago con cada opción?",
        ):
            self.assertTrue(app._isa_asks_for_legend(text), text)

    def test_a_real_answer_is_never_mistaken_for_a_help_request(self):
        # These are things Isa actually types to resolve a case. Reading any
        # of them as "help" would swallow a rejection reason or send the word
        # "ayuda" to a customer instead of her real answer.
        for text in (
            "No hay stock suficiente",
            "¿Podés retirar mañana?",
            "aprobar",
            "No tenemos opciones disponibles en ese color",
            "Necesito que me ayuda con el envío",
            "Preguntale si puede pasar el jueves",
        ):
            self.assertFalse(app._isa_asks_for_legend(text), text)

    def test_legend_explains_both_case_types(self):
        for expected in (
            "Aprobar compra", "Rechazar", "Pedir algo",
            "Responder a Fred", "Seguir conversando", "Cerrar consulta",
        ):
            self.assertIn(expected, app.ISA_OPTIONS_LEGEND)

    def test_the_card_tells_isa_how_to_ask_for_help(self):
        text = app._pending_action_text(_action())
        self.assertIn("AYUDA", text)
        self.assertLessEqual(len(text), 900)

    @patch.object(app, "list_pending_actions")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_asking_for_help_explains_without_touching_the_open_case(
        self, send_message, list_actions,
    ):
        # A rejection is mid-flight: Isa pressed "Rechazar" and Fred is
        # waiting for the motive. Asking for help must explain and leave the
        # case exactly as it was.
        list_actions.return_value = [_action(
            payload={"customer_phone": "5491111111111", "awaiting_isa_kind": "reject_purchase"},
        )]
        with patch.object(app, "_reject_purchase_with_reason") as reject, patch.object(
            app, "_ask_customer_for_purchase",
        ) as ask, patch.object(app, "_deliver_isa_response") as deliver, patch.object(
            app, "resolve_pending_action",
        ) as resolve, patch.object(app, "clear_isa_awaiting") as clear_awaiting:
            app.handle_isa_message("¿qué pasa si apruebo?")
        reject.assert_not_called()
        ask.assert_not_called()
        deliver.assert_not_called()
        resolve.assert_not_called()
        clear_awaiting.assert_not_called()
        self.assertEqual(send_message.call_args.args[1], app.ISA_OPTIONS_LEGEND)


class ConsultationKeepOpenTests(unittest.TestCase):
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "clear_isa_awaiting")
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_reply_keep_open_does_not_close_the_case(
        self, send_message, resolve, clear_awaiting, record, set_state,
    ):
        with patch.object(app, "_isa_customer_instruction", return_value=""):
            handled = app._deliver_isa_response(
                _action(action_type="human_handoff"),
                "Sí, para ese caso recomiendo Taylor cluster.",
                keep_open=True,
            )
        self.assertTrue(handled)
        resolve.assert_not_called()

    @patch.object(app, "pending_action_count", return_value=0)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 7})
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_reply_and_close_resolves_the_case(
        self, send_message, resolve, record, set_state, count,
    ):
        with patch.object(app, "_isa_customer_instruction", return_value=""):
            app._deliver_isa_response(
                _action(action_type="human_handoff"), "Listo, ya está resuelto.",
            )
        resolve.assert_called_once_with(42, "approved")


if __name__ == "__main__":
    unittest.main()
