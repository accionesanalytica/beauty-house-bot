"""Fred's scope after Isa reduced it: informs, never sells, never advises.

    FAQ / policy        -> Knowledge
    existing order      -> Tiendanube live + Knowledge
    concrete product    -> catalog / live, objective answer only
    advice              -> Isa
    purchase            -> Isa
    wholesale info      -> Knowledge
    wholesale purchase  -> Isa

The two rules that carry the most weight, because breaking either is a wrong
answer rather than a slow one:

  * a live order status is not permission to do anything -- "paid" is not
    "packed", and "packed" is not "come and collect it";
  * a cheap word is not an intention. "quiero" does not make a showroom
    question a purchase, and "tienen" does not make it a stock check.
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
import agent  # noqa: E402
from routing_policy import (  # noqa: E402
    DATA_KNOWLEDGE_ONLY,
    DATA_LIVE,
    classify_turn_data_requirement,
)

LEXICON = app.product_lexicon()
KB = {"governing_topic": "pickups_showroom",
      "knowledge_context": "- [politicas / showroom] Texto aprobado."}


def _verdict(message, **extra):
    options = dict(product_lexicon=LEXICON)
    options.update(extra)
    return classify_turn_data_requirement(message, **options)


class ShowroomIsKnowledgeOnlyTests(unittest.TestCase):
    """The production regression: 17.5s and a full catalog+live sweep for a
    policy question, because "quiero" was read as purchase intent."""

    def test_showroom_questions_never_leave_knowledge(self):
        for message in (
            "quiero pasar por el showroom",
            "puedo ir al showroom",
            "cómo funciona el showroom",
            "qué horarios tienen",
            "puedo pasar hoy",
            "cómo retiro por showroom",
        ):
            with self.subTest(message=message):
                verdict = _verdict(message, **KB)
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)
                self.assertEqual(verdict["intent"], "policy_question")

    def test_a_cheap_verb_alone_is_never_a_commercial_signal(self):
        # "quiero" and "tienen" carry no commercial meaning without something
        # commercial attached to them.
        self.assertFalse(app._carries_commercial_object("quiero pasar por el showroom", LEXICON))
        self.assertFalse(app._carries_commercial_object("que horarios tienen", LEXICON))

    def test_but_a_commercial_object_still_wins(self):
        for message, expected in (
            ("¿tenés Isabel I?", "stock_request"),
            ("¿cuánto sale Isabel I?", "price_request"),
            ("quiero comprar dos", "purchase_intent"),
            ("quiero saber el estado de mi pedido", "existing_order"),
        ):
            with self.subTest(message=message):
                self.assertEqual(_verdict(message, **KB)["intent"], expected)


class FredDoesNotAdviseTests(unittest.TestCase):
    def test_asking_which_one_goes_to_isa_without_any_lookup(self):
        for message in (
            "Estoy buscando unas pestañas naturales",
            "¿Qué pestañas me recomendás?",
            "¿Cuál me sirve para lifting?",
            "Quiero algo para ojos pequeños",
            "¿Qué tono me quedaría mejor?",
            "¿Cuál es mejor?",
        ):
            with self.subTest(message=message):
                verdict = _verdict(message)
                self.assertEqual(verdict["intent"], "advice_request")
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_the_handoff_offers_isa_and_recommends_nothing(self):
        reply = app._isa_scope_handoff("¿cuál me recomendás?", {})
        self.assertIn("Isa", reply)
        for forbidden in ("isabel", "foxy", "te recomiendo", "opciones:"):
            self.assertNotIn(forbidden, reply.lower())

    def test_the_discovery_fallback_no_longer_presents_candidates(self):
        # Even reached directly, the candidate-presenting tiers are off.
        self.assertFalse(agent.RECOMMENDATIONS_ENABLED)
        self.assertEqual(
            agent.classify_graceful_discovery_fallback(
                product_discovery_turn=True, handoff_request=None,
                candidates=[{"product_name": "SHOOW TOOLS - ISABEL I", "sku": "X"}],
                has_product_anchor=True,
            ),
            "ask",
        )


class FredDoesNotSellTests(unittest.TestCase):
    def test_purchase_intent_with_a_named_product_goes_to_isa_with_context(self):
        reply = app._isa_scope_handoff("Quiero 4 Isabel I Chocolate", {})
        self.assertIn("Isa", reply)
        self.assertIn("Isabel", reply)
        self.assertIn("4", reply)

    def test_purchase_intent_without_a_product_asks_which_one(self):
        # Quantity alone does not identify anything, so Fred asks rather than
        # handing Isa a request she cannot act on.
        reply = app._isa_scope_handoff("Quiero 4 pestañas", {})
        self.assertNotIn("Isa", reply)
        self.assertIn("?", reply)

    def test_an_active_product_supplies_the_missing_identity(self):
        reply = app._isa_scope_handoff(
            "Me llevo dos", {"active_product_name": "SHOOW TOOLS - ISABEL I"})
        self.assertIn("Isa", reply)
        self.assertIn("ISABEL", reply.upper())

    def test_a_showroom_question_is_never_treated_as_a_purchase(self):
        self.assertEqual(app._isa_scope_handoff("quiero pasar por el showroom", {}), "")

    def test_no_buy_button_is_ever_offered(self):
        # Enforced by the contract, not by configuration: no env var can put
        # Fred back in the selling business.
        self.assertFalse(app._offer_customer_actions(
            7, "549111", "texto", {"active_sku": "X", "active_product_name": "Isabel I"}))


class OrderStateMappingTests(unittest.TestCase):
    """Measured against 40 real orders. UI state comes from the fulfillment,
    never from payment_status."""

    def _reply(self, fulfillment_status, shipping_type, tracking=None, carrier=None):
        return app._render_order_status_reply({
            "order_number": 6345, "payment_status": "paid",
            "shipping_status": "unpacked", "fulfillment_status": fulfillment_status,
            "shipping_type": shipping_type, "tracking": tracking, "carrier": carrier,
        })

    def test_unpacked_says_still_being_prepared_and_quotes_the_window(self):
        reply = self._reply("UNPACKED", "ship").lower()
        self.assertIn("preparación", reply)
        self.assertIn("24 a 72", reply)
        self.assertIn("correo", reply)
        for forbidden in ("ya salió", "listo para retirar", "podés pasar", "no hay problema"):
            self.assertNotIn(forbidden, reply)

    def test_packed_for_shipping_says_packed_and_points_at_the_email(self):
        reply = self._reply("PACKED", "ship").lower()
        self.assertIn("empaquetado", reply)
        self.assertIn("correo", reply)
        self.assertNotIn("ya salió", reply)

    def test_packed_for_pickup_never_says_ready_to_collect(self):
        # PACKED+pickup has not been observed against the Tiendanube UI, so
        # this states only what is certain and asks the customer to wait.
        reply = self._reply("PACKED", "pickup").lower()
        self.assertIn("empaquetado", reply)
        self.assertIn("confirmación", reply)
        for forbidden in ("listo para retirar", "ya podés retirarlo", "te esperamos"):
            self.assertNotIn(forbidden, reply)

    def test_dispatched_reports_carrier_and_tracking_when_they_exist(self):
        reply = self._reply("DISPATCHED", "ship", tracking="AR123", carrier="Envío Nube")
        self.assertIn("despachado", reply.lower())
        self.assertIn("Envío Nube", reply)
        self.assertIn("AR123", reply)
        self.assertIn("1 y 5 días", reply)

    def test_dispatched_without_tracking_invents_none(self):
        reply = self._reply("DISPATCHED", "ship")
        self.assertIn("despachado", reply.lower())
        self.assertNotIn("seguimiento", reply.lower())

    def test_delivered_distinguishes_a_pickup_from_a_delivery(self):
        self.assertIn("retirado", self._reply("DELIVERED", "pickup").lower())
        self.assertIn("entregado", self._reply("DELIVERED", "ship").lower())

    def test_unpaid_never_reports_a_fulfillment_stage(self):
        reply = app._render_order_status_reply({
            "order_number": 1, "payment_status": "pending",
            "fulfillment_status": "UNPACKED", "shipping_type": "ship",
        })
        self.assertIn("no tiene el pago acreditado", reply)

    def test_a_pickup_without_tracking_is_not_an_inconsistency(self):
        # 14 of 21 DELIVERED orders in the real store are pickups with no
        # tracking code. Escalating those would flood Isa with normal orders.
        from routing_policy import _order_status_needs_isa

        class Outcome:
            fact = "order_status"
            status = "completed"
            result = {"found": True, "shipping_type": "pickup",
                      "fulfillment_status": "DELIVERED", "tracking": None}

        self.assertIsNone(_order_status_needs_isa((Outcome(),)))


class OrderLiveObservabilityTests(unittest.TestCase):
    def test_it_reports_the_status_fields_and_no_personal_data(self):
        import re

        stream = io.StringIO()
        with redirect_stdout(stream):
            app.log_order_live({
                "order_number": 6345, "payment_status": "paid",
                "shipping_status": "unpacked", "shipping_type": "ship",
                "fulfillment_status": "UNPACKED", "carrier": "Envío Nube",
                "tracking": None,
                # Fields that must never be logged, present on purpose.
                "contact_name": "Ana", "contact_email": "ana@example.com",
            })
        line = stream.getvalue()
        self.assertIn("[OrderLive]", line)
        for field in ("order", "payment_status", "shipping_status",
                      "shipping_type", "fulfillment_status", "carrier", "tracking"):
            self.assertIn(field + "=", line)
        for leak in ("Ana", "ana@example.com"):
            self.assertNotIn(leak, line)
        self.assertTrue(re.search(r"tracking=(yes|no)", line))


class WholesaleComesFromKnowledgeTests(unittest.TestCase):
    """Isa's approved list lives in Knowledge, never in routing."""

    @classmethod
    def setUpClass(cls):
        from knowledge_rag import load_knowledge_chunks
        cls.chunks = load_knowledge_chunks(BOT_DIR.parent / "knowledge")

    def _context(self, query):
        from knowledge_rag import retrieve_local_knowledge
        return retrieve_local_knowledge(query, self.chunks).context or ""

    def test_the_approved_prices_are_retrievable(self):
        context = self._context("cuánto salen las pestañas por mayor")
        self.assertIn("18.000", context)
        self.assertIn("12 cajas", context)

    def test_the_invoice_and_general_conditions_are_retrievable(self):
        self.assertIn("Factura A", self._context("hacen factura A"))

    def test_no_price_or_minimum_is_hardcoded_in_routing(self):
        import routing_policy

        source = (BOT_DIR / "routing_policy.py").read_text(encoding="utf-8")
        source += (BOT_DIR / "app.py").read_text(encoding="utf-8")
        for literal in ("18.000", "15.000", "120.000", "12 cajas"):
            self.assertNotIn(literal, source)

    def test_no_brand_beyond_the_approved_one_is_named(self):
        context = self._context("con qué marcas trabajan mayorista")
        self.assertNotIn("MAC", context)
        self.assertNotIn("Maybelline", context)

    def test_wanting_to_buy_wholesale_is_still_a_purchase(self):
        self.assertEqual(_verdict("Quiero comprar 12 cajas de Foxy")["intent"],
                         "purchase_intent")


if __name__ == "__main__":
    unittest.main()
