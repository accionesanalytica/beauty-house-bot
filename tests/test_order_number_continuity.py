"""An answer belongs to the question that was just asked.

Real conversation that broke:

    (antes)  "El protector solar de tocobo, llegó abierto..."
    clienta  "hola fred quiero retirar un pedido"
    Fred     pide el número de orden
    clienta  "6295"

Three failures, one shape. "quiero retirar un pedido" read as purchase_intent
because of the "quiero", so the tracking flow never started. Then "6295"
arrived as a fresh turn whose meaning was rebuilt from surrounding history,
which picked up the earlier damaged-product complaint and pulled the wrong
Knowledge into an order lookup. And the model, holding a live status of "paid,
being prepared", concluded the order could simply be collected.

A live order status is not authorisation to collect. Per
knowledge/procedures/pickups.md a pickup starts with a reservation, and
availability is never confirmed without Isa.
"""

import asyncio
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
from routing_policy import (  # noqa: E402
    DATA_KNOWLEDGE_ONLY,
    DATA_LIVE,
    classify_turn_data_requirement,
)

OLD_COMPLAINT = (
    "El protector solar de tocobo, llegó abierto y con la mitad del contenido "
    "derramado dentro de la caja"
)


class CollectingAnOrderIsNotShoppingTests(unittest.TestCase):
    def test_wanting_to_collect_an_order_is_an_existing_order_turn(self):
        for message in (
            "hola fred quiero retirar un pedido",
            "quiero retirar mi pedido",
            "paso a buscar la compra",
            "puedo retirar la orden hoy?",
        ):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message, product_lexicon=app.product_lexicon())
                self.assertEqual(verdict["intent"], "existing_order")
                self.assertEqual(verdict["data_required"], DATA_LIVE)

    def test_the_generic_pickup_policy_question_is_still_knowledge_only(self):
        # The distinction is the order noun: this one names no order, so the
        # approved showroom policy answers it and nothing live is needed.
        for message in ("¿Puedo retirar por el showroom?",
                        "¿Cómo es el retiro en el showroom?"):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message,
                    governing_topic="pickups_showroom",
                    knowledge_context="- [politicas / showroom] Texto aprobado.",
                    product_lexicon=app.product_lexicon(),
                )
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)


class OrderNumberAnswersTheQuestionAskedTests(unittest.TestCase):
    def test_frets_own_prompt_counts_as_asking(self):
        self.assertTrue(app._fred_just_asked_for_order_number(
            [{"role": "assistant", "content": app.ORDER_NUMBER_PROMPT_TEXT}]))

    def test_the_model_asking_in_its_own_words_also_counts(self):
        # This is the case the TRACKING flag missed: the deterministic prompt
        # sets state, free-form model text does not.
        for wording in (
            "¡Claro! ¿Me pasás el número de orden así lo reviso?",
            "Decime el nro de pedido y lo busco 😊",
            "¿Tenés el número de pedido a mano?",
        ):
            with self.subTest(wording=wording):
                self.assertTrue(app._fred_just_asked_for_order_number(
                    [{"role": "assistant", "content": wording}]))

    def test_only_the_last_message_counts(self):
        # An order-number request from earlier was already answered or
        # abandoned; treating a number as its answer is the same bug again.
        history = [
            {"role": "assistant", "content": app.ORDER_NUMBER_PROMPT_TEXT},
            {"role": "user", "content": "6295"},
            {"role": "assistant", "content": "Tu pedido está en preparación."},
        ]
        self.assertFalse(app._fred_just_asked_for_order_number(history))

    def test_an_unrelated_last_message_does_not_count(self):
        self.assertFalse(app._fred_just_asked_for_order_number(
            [{"role": "assistant", "content": "Tenemos Isabel I en varias opciones."}]))
        self.assertFalse(app._fred_just_asked_for_order_number([]))


class PickupIsNeverAuthorisedByStatusTests(unittest.TestCase):
    def test_a_pickup_request_is_recognised_from_the_previous_turn(self):
        self.assertTrue(app._pickup_requested(
            "6295", [{"role": "user", "content": "quiero retirar un pedido"}]))

    def test_a_plain_tracking_question_is_not_a_pickup(self):
        self.assertFalse(app._pickup_requested(
            "6295", [{"role": "user", "content": "¿dónde está mi pedido?"}]))

    def test_the_pickup_step_asks_for_a_booking_and_promises_nothing(self):
        step = app._pickup_next_step()
        self.assertIn("reservar", step.lower())
        self.assertIn("Isa", step)
        for forbidden in ("podés pasar", "ya podés retirar", "está listo",
                          "no hay problema", "cuando quieras"):
            self.assertNotIn(forbidden, step.lower())


def _state_factory(store):
    def get_state(conversation_id):
        base = {
            "mode": "CHAT", "active_product_id": None, "active_product_name": None,
            "active_sku": None, "active_variant": None, "unit_price": None,
            "quantity": None, "delivery_method": None, "customer_name": None,
            "customer_email": None, "postal_code": None, "checkout_step": None,
            "order_number": None,
        }
        base.update(store)
        return base
    return get_state


class Request:
    def __init__(self, text, message_id):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "5491111111111", "id": message_id, "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


class TheWholeConversationTests(unittest.TestCase):
    """The exact production exchange, end to end."""

    ORDER = {
        "order_number": "6295", "tracking": None,
        "payment_status": "paid", "shipping_status": "unpacked",
    }

    def _run(self, messages, order_result=None):
        store, sent, model_calls, catalog_queries = {}, [], [], []
        history = [
            {"role": "user", "content": OLD_COMPLAINT},
            {"role": "assistant", "content": "Lo lamento muchísimo, lo vemos con Isa."},
        ]

        def send(phone, text):
            sent.append(text)
            history.append({"role": "assistant", "content": text})
            return True

        def save_state(conversation_id, **fields):
            store.update({k: v for k, v in fields.items() if v is not None})

        mocks = {
            "CONVERSATION_DEBOUNCE_SECONDS": 0,
            "BOT_RESPONSE_MODE": "agent",
            "get_fred_core_state": _state_factory(store),
            "save_fred_core_state": save_state,
            "reset_fred_core_checkout": lambda c: None,
            "get_active_sales_intake": lambda c: None,
            "get_product_selection": lambda *a, **k: None,
            "record_inbound_message": lambda *a, **k: (7, "BOT", False),
            "record_bot_message": lambda *a, **k: None,
            "record_agent_turn": lambda **k: None,
            "load_history": lambda *a, **k: list(history),
            "is_latest_customer_message": lambda *a, **k: True,
            "send_whatsapp_text": send,
            "send_customer_action_buttons": lambda *a, **k: True,
            "get_order_status": lambda number: dict(
                order_result or self.ORDER, order_number=number),
            "search_similar_products": lambda *a, **k: catalog_queries.append(a) or "",
            "answer": lambda text, **kwargs: model_calls.append(
                {"message": text, "rag_context": kwargs.get("rag_context", "")}
            ) or {
                "reply": "(modelo)", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
        }
        handles = [patch.object(app, name, value) for name, value in mocks.items()]
        for handle in handles:
            handle.start()
        try:
            for index, text in enumerate(messages):
                with redirect_stdout(io.StringIO()):
                    asyncio.run(app.webhook_post(Request(text, "wamid-{}".format(index))))
                history.append({"role": "user", "content": text})
        finally:
            for handle in reversed(handles):
                handle.stop()
        return sent, model_calls, catalog_queries, store

    def test_the_full_exchange_looks_up_the_order_and_asks_for_a_booking(self):
        sent, model_calls, catalog_queries, store = self._run(
            ["hola fred quiero retirar un pedido", "6295"])

        self.assertEqual(sent[0], app.ORDER_NUMBER_PROMPT_TEXT)
        self.assertIn("#6295", sent[1])
        self.assertIn("en preparación", sent[1])
        # Status reported, booking requested, nothing authorised.
        self.assertIn("reservar", sent[1].lower())

        # No model on either turn: both are deterministic lookups.
        self.assertEqual(model_calls, [])
        # And no catalog retrieval at all -- the bug had one on each turn.
        self.assertEqual(catalog_queries, [])
        self.assertEqual(store.get("order_number"), "6295")

    def test_no_trace_of_the_earlier_complaint_reaches_the_answer(self):
        sent, model_calls, _, _ = self._run(
            ["hola fred quiero retirar un pedido", "6295"])
        combined = " ".join(sent).lower()
        for leak in ("protector", "tocobo", "abierto", "derramado", "dañado"):
            self.assertNotIn(leak, combined)
        self.assertEqual(model_calls, [])

    def test_the_status_is_reported_but_never_read_as_permission(self):
        sent, _, _, _ = self._run(["hola fred quiero retirar un pedido", "6295"])
        reply = sent[1].lower()
        for inference in ("no hay problema", "ya podés retirarlo", "está listo",
                          "podés pasar cuando quieras", "está disponible para retirar"):
            self.assertNotIn(inference, reply)

    def test_a_bare_number_nobody_asked_for_is_not_an_order_number(self):
        # The guard must not swallow numbers in a normal conversation --
        # "6295" only means an order when Fred just asked for one.
        sent, model_calls, _, _ = self._run(["hola, tienen algo lindo?", "6295"])
        self.assertTrue(model_calls, "el turno debía seguir el camino normal")

    def test_a_delivered_order_still_reports_its_real_state(self):
        sent, _, _, _ = self._run(
            ["quiero retirar un pedido", "6295"],
            order_result={
                "order_number": "6295", "tracking": "AR123456789",
                "payment_status": "paid", "shipping_status": "shipped",
            },
        )
        self.assertIn("AR123456789", sent[1])


if __name__ == "__main__":
    unittest.main()


class GenericPickupPolicyIsNotATrackingTurnTests(unittest.TestCase):
    """Asking HOW pickups work is not asking about one order.

    The regression: keying on "retirar + pedido" sent "hola como puedo retirar
    un pedido" into TRACKING, so a policy question was answered with a request
    for an order number the customer never had in mind.
    """

    def _tracking(self, message):
        from knowledge_rag import _normalise as knowledge_normalise
        return app._pickup_of_a_specific_order(knowledge_normalise(message))

    def test_asking_how_a_pickup_works_is_never_tracking(self):
        for message in (
            "¿Cómo puedo retirar un pedido?",
            "hola como puedo retirar un pedido",
            "cómo retiro un pedido",
            "¿cómo funciona el retiro?",
            "¿qué necesito para retirar un pedido?",
            "¿qué hace falta para retirar una compra?",
            "¿se puede retirar un pedido en el showroom?",
        ):
            with self.subTest(message=message):
                self.assertFalse(self._tracking(message))

    def test_wanting_to_collect_a_specific_order_is_tracking(self):
        for message in (
            "Quiero retirar mi pedido",
            "quiero retirar mi compra",
            "paso a buscar el pedido",
            "necesito retirar mi orden",
        ):
            with self.subTest(message=message):
                self.assertTrue(self._tracking(message))

    def test_a_named_order_wins_even_when_phrased_as_a_procedure(self):
        # "¿cómo retiro el pedido 6295?" names an order, so its real state
        # still matters -- the procedure wording does not erase that.
        for message in ("¿cómo retiro el pedido 6295?",
                        "como hago para retirar mi pedido"):
            with self.subTest(message=message):
                self.assertTrue(self._tracking(message))

    def test_the_generic_question_stays_a_knowledge_turn(self):
        for message in ("¿Cómo puedo retirar un pedido?",
                        "¿qué necesito para retirar?"):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message,
                    governing_topic="pickups_showroom",
                    knowledge_context="- [politicas / showroom] Texto aprobado.",
                    product_lexicon=app.product_lexicon(),
                )
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)


class TrackingReleasesInsteadOfHoldingTests(unittest.TestCase):
    """Waiting for an order number is waiting, not owning the conversation."""

    def _run_from_tracking(self, message):
        store, sent, model_calls = {"mode": "TRACKING"}, [], []

        def get_state(conversation_id):
            base = {
                "mode": "CHAT", "active_product_id": None, "active_product_name": None,
                "active_sku": None, "active_variant": None, "unit_price": None,
                "quantity": None, "delivery_method": None, "customer_name": None,
                "customer_email": None, "postal_code": None, "checkout_step": None,
                "order_number": None,
            }
            base.update(store)
            return base

        mocks = {
            "CONVERSATION_DEBOUNCE_SECONDS": 0, "BOT_RESPONSE_MODE": "agent",
            "get_fred_core_state": get_state,
            "save_fred_core_state": lambda c, **f: store.update(
                {k: v for k, v in f.items() if v is not None}),
            "reset_fred_core_checkout": lambda c: None,
            "get_active_sales_intake": lambda c: None,
            "get_product_selection": lambda *a, **k: None,
            "record_inbound_message": lambda *a, **k: (7, "BOT", False),
            "record_bot_message": lambda *a, **k: None,
            "record_agent_turn": lambda **k: None,
            "load_history": lambda *a, **k: [],
            "is_latest_customer_message": lambda *a, **k: True,
            "send_whatsapp_text": lambda phone, text: sent.append(text) or True,
            "send_customer_action_buttons": lambda *a, **k: True,
            "get_order_status": lambda n: {
                "order_number": n, "tracking": None,
                "payment_status": "paid", "shipping_status": "unpacked"},
            "search_similar_products": lambda *a, **k: "",
            "_live_candidate_context": lambda *a, **k: "",
            "answer": lambda text, **kwargs: model_calls.append(text) or {
                "reply": "(respuesta normal)", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"}},
        }
        handles = [patch.object(app, name, value) for name, value in mocks.items()]
        for handle in handles:
            handle.start()
        try:
            with redirect_stdout(io.StringIO()):
                asyncio.run(app.webhook_post(Request(message, "w0")))
        finally:
            for handle in reversed(handles):
                handle.stop()
        return sent, model_calls, store

    def test_a_number_is_still_treated_as_the_order_number(self):
        sent, model_calls, store = self._run_from_tracking("6295")
        self.assertIn("#6295", sent[0])
        self.assertEqual(model_calls, [])

    def test_cancelar_leaves_tracking_and_is_answered_in_the_same_turn(self):
        sent, model_calls, store = self._run_from_tracking("cancelar")
        self.assertEqual(store["mode"], "CHAT")
        self.assertTrue(model_calls, "el mensaje debía procesarse normalmente")
        self.assertNotIn(app.ORDER_NUMBER_PROMPT_TEXT, " ".join(sent))

    def test_changing_the_subject_is_answered_without_a_second_message(self):
        # The whole point: the customer must not have to repeat themselves.
        sent, model_calls, store = self._run_from_tracking("hola quiero pasar al showroom")
        self.assertEqual(store["mode"], "CHAT")
        self.assertEqual(model_calls, ["hola quiero pasar al showroom"])
        self.assertTrue(sent)

    def test_a_plain_greeting_does_not_stay_stuck(self):
        # "hola" is answered by the social shortcut without a model call, so
        # what matters here is that TRACKING let go and the greeting got a
        # greeting -- not another request for a number.
        for message in ("hola", "tengo otra pregunta"):
            with self.subTest(message=message):
                sent, _, store = self._run_from_tracking(message)
                self.assertEqual(store["mode"], "CHAT")
                self.assertTrue(sent)
                self.assertNotIn("número de orden", " ".join(sent).lower())

    def test_leaving_tracking_never_answers_with_the_order_prompt(self):
        for message in ("cancelar", "hola", "quiero pasar al showroom",
                        "tengo otra pregunta"):
            with self.subTest(message=message):
                sent, _, _ = self._run_from_tracking(message)
                self.assertNotIn(
                    "número de orden", " ".join(sent).lower(),
                    "salir de TRACKING no debe volver a pedir el número")
