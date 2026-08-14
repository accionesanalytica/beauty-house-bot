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

    @patch.object(app, "send_whatsapp_text", return_value=False)
    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=0)
    def test_queue_reports_false_when_every_channel_fails(
        self, count, set_state, create_action, send_text,
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

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=0)
    def test_plain_text_summary_rescues_a_failed_card(
        self, count, set_state, create_action, send_text,
    ):
        # A transient card failure inside an open window must still reach Isa.
        with patch.object(app, "send_isa_pending_buttons", return_value=False), patch.object(
            app, "send_isa_pending_notification", return_value=False,
        ):
            notified = app._queue_for_isa(
                7, "5491111111111", "purchase_review", "resumen", "mensaje",
            )
        self.assertTrue(notified)
        self.assertIn("Pendiente #99", send_text.call_args.args[1])

    @patch.object(app, "send_whatsapp_text", return_value=False)
    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=0)
    def test_queue_reports_true_when_the_card_reaches_isa(
        self, count, set_state, create_action, send_text,
    ):
        with patch.object(app, "send_isa_pending_buttons", return_value=True):
            self.assertTrue(app._queue_for_isa(
                7, "5491111111111", "human_handoff", "resumen", "mensaje",
            ))

    @patch.object(app, "send_whatsapp_text", return_value=False)
    @patch.object(app, "create_pending_action", return_value=99)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "pending_action_count", return_value=3)
    def test_template_is_not_resent_when_isa_already_has_a_queue(
        self, count, set_state, create_action, send_text,
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


class ReasonForCustomerTests(unittest.TestCase):
    """Isa instructs Fred; Fred writes for the customer. Her instruction must
    never be copied through verbatim (production sent "Ok envíale al cliente
    que no hay stock" straight to a customer)."""

    def test_instruction_wrapper_is_stripped(self):
        cases = {
            "Ok envíale al cliente que no hay stock": "No hay stock",
            "envíale que no tenemos stock suficiente": "No tenemos stock suficiente",
            "decile que llega el martes": "Llega el martes",
            "avisá al cliente que se demora una semana": "Se demora una semana",
        }
        for written, expected in cases.items():
            self.assertEqual(app._reason_for_customer(written), expected)

    def test_a_plain_reason_is_left_untouched(self):
        for text in (
            "No hay stock suficiente para esa cantidad",
            "El producto viene con 3 grupos de fibras",
        ):
            self.assertEqual(app._reason_for_customer(text), text)


class PurchaseDraftIntegrityTests(unittest.TestCase):
    """A purchase may never be summarised, escalated or approved unless its
    product identity is real and sellable right now."""

    def test_a_draft_without_a_real_sku_is_rejected(self):
        for sku in ("", None, "a confirmar"):
            self.assertEqual(
                app._purchase_draft_integrity_error({"selected_sku": sku, "quantity": 2}),
                "sin SKU real",
            )

    @patch.object(app, "get_stock", return_value={"found": False})
    def test_a_sku_the_store_does_not_know_is_an_integrity_error(self, get_stock):
        error = app._purchase_draft_integrity_error({"selected_sku": "GHOST-1", "quantity": 1})
        self.assertIn("no existe", error)

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 1,
    })
    def test_insufficient_stock_is_reported_as_stock_not_identity(self, get_stock):
        error = app._purchase_draft_integrity_error({"selected_sku": "REAL-1", "quantity": 5})
        self.assertIn("stock insuficiente", error)

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 40,
    })
    def test_a_real_sellable_draft_passes(self, get_stock):
        self.assertEqual(
            app._purchase_draft_integrity_error({"selected_sku": "REAL-1", "quantity": 2}), "",
        )


class CheckoutFailureClassificationTests(unittest.TestCase):
    """"No hay stock" and "este borrador apunta a un producto inexistente"
    need different actions from Isa and must never be conflated."""

    def test_missing_sku_is_named_an_integrity_error(self):
        message = app._classify_checkout_failure({"selected_sku": "a confirmar"}, ValueError("x"))
        self.assertIn("integridad", message.lower())
        self.assertNotIn("no hay stock", message.lower())

    @patch.object(app, "get_stock", return_value={"found": False})
    def test_unknown_sku_is_named_an_integrity_error(self, get_stock):
        message = app._classify_checkout_failure({"selected_sku": "GHOST-1"}, ValueError("x"))
        self.assertIn("integridad", message.lower())

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "quantity": 40,
    })
    def test_a_healthy_product_is_never_called_a_stock_problem(self, get_stock):
        message = app._classify_checkout_failure({"selected_sku": "REAL-1"}, ValueError("boom"))
        self.assertIn("no es falta de", message.lower())


class IsaOwnsConversationTests(unittest.TestCase):
    """Seguir conversación hands the thread to Isa: Fred goes silent and only
    carries messages until she hands it back."""

    @patch.object(app, "set_conversation_state")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_isa_awaiting")
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_keep_open_takes_ownership_and_announces_the_handover(
        self, send_message, resolve, set_awaiting, record, set_state,
    ):
        with patch.object(app, "_isa_customer_instruction", return_value=""):
            app._deliver_isa_response(
                _action(action_type="human_handoff"),
                "Contame qué buscás y te ayudo.",
                keep_open=True,
            )
        # The case stays open, marked as owned, and the conversation is muted.
        resolve.assert_not_called()
        set_awaiting.assert_called_once_with(42, app.ISA_OWNS_KIND)
        set_state.assert_called_once_with(7, "ISA")
        to_customer = send_message.call_args_list[0].args[1]
        self.assertIn("Te dejo con Isa", to_customer)
        self.assertIn("Contame qué buscás", to_customer)

    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "list_pending_actions")
    def test_isa_text_reaches_the_customer_verbatim(
        self, list_actions, send_message, record,
    ):
        list_actions.return_value = [_action(
            action_type="human_handoff",
            payload={"customer_phone": "5491111111111", "awaiting_isa_kind": app.ISA_OWNS_KIND},
        )]
        written = "Para lo que buscás yo te recomendaría las Foxy."
        app.handle_isa_message(written)
        sent = send_message.call_args.args
        self.assertEqual(sent[0], "5491111111111")
        # Exactly what she wrote: no prefix, no rewriting.
        self.assertEqual(sent[1], written)

    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "resolve_pending_action", return_value={"conversation_id": 7})
    @patch.object(app, "clear_isa_awaiting")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "list_pending_actions")
    def test_devolver_a_fred_gives_the_thread_back(
        self, list_actions, send_message, clear_awaiting, resolve, set_state, record,
    ):
        list_actions.return_value = [_action(
            action_type="human_handoff",
            payload={"customer_phone": "5491111111111", "awaiting_isa_kind": app.ISA_OWNS_KIND},
        )]
        app.handle_isa_message("devolver a Fred")
        set_state.assert_called_once_with(7, "BOT")
        clear_awaiting.assert_called_once_with(42)
        resolve.assert_called_once_with(42, "approved")

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "list_pending_actions")
    def test_asking_for_help_while_owning_does_not_reach_the_customer(
        self, list_actions, send_message,
    ):
        list_actions.return_value = [_action(
            action_type="human_handoff",
            payload={"customer_phone": "5491111111111", "awaiting_isa_kind": app.ISA_OWNS_KIND},
        )]
        app.handle_isa_message("AYUDA")
        recipients = [call.args[0] for call in send_message.call_args_list]
        self.assertNotIn("5491111111111", recipients)
        self.assertEqual(send_message.call_args.args[1], app.ISA_OPTIONS_LEGEND)


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
            "Responder y cerrar", "Seguir conversación", "Devolver a Fred",
            "Cerrar consulta",
        ):
            self.assertIn(expected, app.ISA_OPTIONS_LEGEND)
        # The legend must be explicit that taking the chat silences Fred.
        self.assertIn("yo no respondo nada", app.ISA_OPTIONS_LEGEND)

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
