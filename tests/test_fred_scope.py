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

class _Request:
    def __init__(self, text, message_id="wamid-scope"):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "5491111111111", "id": message_id, "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


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


class PolicyBypassSkipsCatalogAndStoreTests(unittest.TestCase):
    """The one bypass, and only it.

    A turn an approved topic fully governs cannot be improved by the catalog
    or the store, so neither is consulted. Everything else keeps every lookup
    it had -- the bypass is narrow by construction, because every commercial
    branch is checked before policy_question can be reached.
    """

    def _run(self, message, topic="pickups_showroom", context="- [x] Texto aprobado."):
        store, calls = {"mode": "CHAT"}, {"catalog": 0, "live": 0}

        def get_state(conversation_id):
            base = dict.fromkeys([
                "active_product_id", "active_product_name", "active_sku",
                "active_variant", "unit_price", "quantity", "delivery_method",
                "customer_name", "customer_email", "postal_code",
                "checkout_step", "order_number",
            ])
            base["mode"] = "CHAT"
            base.update(store)
            return base

        class Bundle:
            governing_topic = topic
            dynamic_requirements = ()

            def __init__(self):
                self.context = context

        def catalog(*args, **kwargs):
            calls["catalog"] += 1
            return "- Producto | product_id: 1"

        def live(*args, **kwargs):
            calls["live"] += 1
            return ""

        mocks = {
            "CONVERSATION_DEBOUNCE_SECONDS": 0, "BOT_RESPONSE_MODE": "agent",
            "KNOWLEDGE_RAG_ENABLED": True,
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
            "send_whatsapp_text": lambda p, t: True,
            "send_customer_action_buttons": lambda *a, **k: True,
            "embed_text": lambda *a, **k: [0.0] * 768,
            "search_similar_products": catalog,
            "_live_candidate_context": live,
            "retrieve_with_recent_context": lambda m, h, fn: (Bundle(), m, None),
            "execute_dynamic_requirements": lambda *a, **k: (),
            "get_order_status": lambda n: {
                "found": True, "order_number": n, "payment_status": "paid",
                "fulfillment_status": "UNPACKED", "shipping_type": "ship",
                "shipping_status": "unpacked", "tracking": None, "carrier": None},
            "answer": lambda t, **k: {
                "reply": "(respuesta)", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"}},
        }
        handles = [patch.object(app, name, value) for name, value in mocks.items()]
        for handle in handles:
            handle.start()
        stream = io.StringIO()
        try:
            with redirect_stdout(stream):
                asyncio.run(app.webhook_post(_Request(message)))
        finally:
            for handle in reversed(handles):
                handle.stop()
        return calls, stream.getvalue()

    def test_a_showroom_question_touches_neither_catalog_nor_the_store(self):
        calls, output = self._run("quiero pasar por el showroom")
        self.assertEqual(calls["catalog"], 0)
        self.assertEqual(calls["live"], 0)
        self.assertIn("intent=policy_question", output)
        self.assertIn("data_required=knowledge_only", output)
        self.assertIn("skipped_live=true", output)
        self.assertIn("catalog_ms=0", output)
        self.assertIn("live_stock_ms=0", output)

    def test_the_bypass_never_applies_to_a_commercial_turn(self):
        # The five regressions: each names a product, a price, stock or one
        # specific order, so each must keep its lookups.
        for message in (
            "¿Tienen Isabel I Chocolate?",
            "¿Cuánto sale Isabel I?",
            "¿Hay stock?",
            "quiero saber el estado de mi pedido",
            "pedido 6345",
        ):
            with self.subTest(message=message):
                calls, output = self._run(message)
                self.assertNotIn("intent=policy_question", output)
                if "[FredTiming]" in output:
                    self.assertGreater(
                        calls["catalog"] + calls["live"], 0,
                        "un turno comercial no puede saltear catálogo y tienda")

    def test_advice_never_reaches_the_bypass_even_though_it_is_knowledge_only(self):
        # advice_request is knowledge_only too. It is handed to Isa before any
        # retrieval, so it never gets as far as this branch -- the bypass is
        # not "skip work whenever data_required is knowledge_only".
        calls, output = self._run("¿cuál me recomendás?")
        self.assertEqual(calls["catalog"], 0)
        self.assertEqual(calls["live"], 0)
        self.assertNotIn("[FredTiming]", output)

    def test_without_a_governing_topic_nothing_is_skipped(self):
        calls, _ = self._run("una consulta cualquiera", topic=None)
        self.assertGreater(calls["catalog"], 0)


class AnOrderNamedByItsNumberNeedsNothingElseTests(unittest.TestCase):
    """"pedido 6345" carries its own identifier.

    Nothing has to be inferred from surrounding words, from history, or from
    the catalog -- and none of those can add anything to it. It used to fall
    through to the generic path and pay for a catalog search and a live
    product lookup to answer a question the order number already answered.
    """

    def test_every_explicit_form_classifies_as_an_existing_order(self):
        for message in ("pedido 6345", "orden 6345", "pedido #6345", "orden #6345",
                        "Pedido 6345", "hola, orden #6345"):
            with self.subTest(message=message):
                verdict = _verdict(message, **KB)
                self.assertEqual(verdict["intent"], "existing_order")
                self.assertEqual(verdict["data_required"], DATA_LIVE)

    def test_the_number_is_read_from_the_message_not_hardcoded(self):
        from routing_policy import order_number_reference

        for message, expected in (("pedido 6345", "6345"), ("orden #12", "12"),
                                  ("pedido 999999", "999999")):
            with self.subTest(message=message):
                self.assertEqual(order_number_reference(message), expected)

    def test_a_bare_number_or_an_unrelated_one_is_not_an_order_reference(self):
        from routing_policy import order_number_reference

        for message in ("6345", "quiero 12 cajas", "el 5 de mayo", "pedido"):
            with self.subTest(message=message):
                self.assertEqual(order_number_reference(message), "")

    def test_it_looks_the_order_up_directly_without_catalog_or_product_stock(self):
        looked_up, calls = [], {"catalog": 0, "live": 0}
        store = {"mode": "CHAT"}

        def get_state(conversation_id):
            base = dict.fromkeys([
                "active_product_id", "active_product_name", "active_sku",
                "active_variant", "unit_price", "quantity", "delivery_method",
                "customer_name", "customer_email", "postal_code",
                "checkout_step", "order_number",
            ])
            base["mode"] = "CHAT"
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
            # Deliberately non-empty: an old complaint must not be consulted.
            "load_history": lambda *a, **k: [
                {"role": "user", "content": "el protector llegó abierto"}],
            "is_latest_customer_message": lambda *a, **k: True,
            "send_whatsapp_text": lambda p, t: True,
            "search_similar_products": lambda *a, **k: (
                calls.__setitem__("catalog", calls["catalog"] + 1) or ""),
            "_live_candidate_context": lambda *a, **k: (
                calls.__setitem__("live", calls["live"] + 1) or ""),
            "answer": lambda t, **k: {
                "reply": "(modelo)", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"}},
            "get_order_status": lambda n: looked_up.append(n) or {
                "found": True, "order_number": n, "payment_status": "paid",
                "shipping_status": "unpacked", "fulfillment_status": "UNPACKED",
                "shipping_type": "ship", "tracking": None, "carrier": None},
        }
        for message, expected in (("pedido 6345", "6345"), ("orden 6345", "6345"),
                                  ("pedido #6345", "6345")):
            with self.subTest(message=message):
                looked_up.clear()
                calls["catalog"] = calls["live"] = 0
                store.clear()
                store["mode"] = "CHAT"
                handles = [patch.object(app, n, v) for n, v in mocks.items()]
                for handle in handles:
                    handle.start()
                stream = io.StringIO()
                try:
                    with redirect_stdout(stream):
                        asyncio.run(app.webhook_post(_Request(message)))
                finally:
                    for handle in reversed(handles):
                        handle.stop()
                self.assertEqual(looked_up, [expected])
                self.assertEqual(calls["catalog"], 0, "no debe consultar catálogo")
                self.assertEqual(calls["live"], 0, "no debe verificar stock de producto")
                self.assertIn("[OrderLive]", stream.getvalue())

    def test_it_works_without_fred_having_just_asked_for_a_number(self):
        # No prior assistant message at all: the reference stands alone.
        self.assertFalse(app._fred_just_asked_for_order_number([]))
        self.assertEqual(_verdict("pedido 6345")["intent"], "existing_order")


class WholesaleConditionsDoNotMixTests(unittest.TestCase):
    """SHOOW TOOLS has its own approved minimums; the generic ones must not be
    read as applying to it."""

    @classmethod
    def setUpClass(cls):
        from knowledge_rag import load_knowledge_chunks
        cls.chunks = load_knowledge_chunks(BOT_DIR.parent / "knowledge")

    def _context(self, query):
        from knowledge_rag import retrieve_local_knowledge
        return retrieve_local_knowledge(query, self.chunks).context or ""

    def test_the_shoow_tools_minimum_is_the_approved_one(self):
        self.assertIn("12 cajas", self._context(
            "cuál es el mínimo de pestañas SHOOW TOOLS mayorista"))

    def test_the_shoow_tools_price_is_the_approved_one(self):
        self.assertIn("18.000", self._context(
            "cuánto salen las pestañas SHOOW TOOLS mayorista"))

    def test_the_approved_brand_list_is_untouched(self):
        context = self._context("qué marcas trabajan por mayor")
        for brand in ("GOT2B", "Kleancolor", "Moira"):
            self.assertIn(brand, context)

    def test_wherever_the_generic_minimum_is_stated_it_says_it_is_generic(self):
        # The mixing guard, checked at the source rather than per query: any
        # chunk that states the generic minimum alongside SHOOW TOOLS content
        # must also say which one governs.
        import re
        from pathlib import Path

        scoped = "rigen exclusivamente las condiciones de su sección"
        for path in (BOT_DIR.parent / "knowledge").rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            if "3, 6 o 12 unidades" in text:
                with self.subTest(path=path.name):
                    self.assertIn(scoped, text)
