"""A social message is answered as a social message, whatever came before.

Real case: after several showroom turns, "hola que tal" came back with the
showroom policy. Two separate defects produced it.

  1. The social shortcut did not recognise "hola que tal", so the turn fell
     through to the full pipeline.
  2. Retrieval, finding no topic in the greeting, widened its query into the
     recent conversation and inherited `pickups_showroom` from turns the
     customer had already finished with.

The contract these tests hold: for a self-sufficient message the current turn
decides, and a greeting is self-sufficient -- it means the greeting. History
is NOT switched off globally; an elliptical turn ("quiero 4") still needs it.
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
from knowledge_rag import (  # noqa: E402
    KnowledgeRetrieval,
    retrieve_with_recent_context,
)
from routing_policy import message_is_self_sufficient  # noqa: E402


class _Request:
    def __init__(self, text):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "5491111111111", "id": "wamid-social", "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


# A conversation that was entirely about the showroom, ending on Fred's own
# policy answer -- the exact shape that leaked into the greeting.
SHOWROOM_HISTORY = [
    {"role": "user", "content": "quiero pasar por el showroom"},
    {"role": "assistant", "content": (
        "El showroom está cerrado al público; sólo se realizan retiros "
        "previamente coordinados con reserva previa."
    )},
    {"role": "user", "content": "quiero pasar por el showroom"},
    {"role": "assistant", "content": (
        "El showroom está cerrado al público; sólo se realizan retiros "
        "previamente coordinados con reserva previa."
    )},
]


class SocialTurnSpendsNothingTests(unittest.TestCase):
    """End-to-end: greeting after a showroom conversation."""

    def _turn(self, text, history):
        sent = []
        calls = {"knowledge": 0, "model": 0, "catalog": 0, "live": 0,
                 "recycle_guard": 0, "fallback": 0}

        def counted(key, result):
            def _spy(*args, **kwargs):
                calls[key] += 1
                return result
            return _spy

        def get_state(_conversation_id):
            base = dict.fromkeys([
                "active_product_id", "active_product_name", "active_sku",
                "active_variant", "unit_price", "quantity", "delivery_method",
                "customer_name", "customer_email", "postal_code",
                "checkout_step", "order_number",
            ])
            base["mode"] = "CHAT"
            return base

        mocks = {
            "CONVERSATION_DEBOUNCE_SECONDS": 0, "BOT_RESPONSE_MODE": "agent",
            "KNOWLEDGE_RAG_ENABLED": True,
            "get_fred_core_state": get_state,
            "save_fred_core_state": lambda c, **f: None,
            "reset_fred_core_checkout": lambda c: None,
            "get_active_sales_intake": lambda c: None,
            "get_product_selection": lambda *a, **k: None,
            "record_inbound_message": lambda *a, **k: (7, "BOT", False),
            "record_bot_message": lambda *a, **k: None,
            "record_agent_turn": lambda **k: None,
            "load_history": lambda *a, **k: list(history),
            "is_latest_customer_message": lambda *a, **k: True,
            "send_whatsapp_text": lambda p, t: sent.append(t) or True,
            "embed_text": counted("knowledge", [0.0] * 768),
            "search_similar_products": counted("catalog", ""),
            "_live_candidate_context": counted("catalog", ""),
            "get_order_status": counted("live", {"found": False}),
            "get_stock": counted("live", {"found": False}),
            "retrieve_with_recent_context": counted(
                "knowledge", (KnowledgeRetrieval(), text, False)
            ),
            "execute_dynamic_requirements": lambda *a, **k: (),
            "answer": counted("model", {
                "reply": "respuesta del modelo", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"}}),
            "_reply_belongs_to_this_turn": counted("recycle_guard", "x"),
            "_send_service_fallback": counted("fallback", None),
        }
        handles = [patch.object(app, name, value) for name, value in mocks.items()]
        for handle in handles:
            handle.start()
        stream = io.StringIO()
        try:
            with redirect_stdout(stream):
                asyncio.run(app.webhook_post(_Request(text)))
        finally:
            for handle in reversed(handles):
                handle.stop()
        return (sent[0] if sent else ""), calls, stream.getvalue()

    def test_a_greeting_after_a_showroom_conversation_is_answered_socially(self):
        reply, _, _ = self._turn("hola que tal", SHOWROOM_HISTORY)
        self.assertIn("¿En qué te puedo ayudar?", reply)
        self.assertNotIn("showroom", reply.lower())
        self.assertNotIn("reserva", reply.lower())

    def test_the_greeting_spends_nothing(self):
        _, calls, _ = self._turn("hola que tal", SHOWROOM_HISTORY)
        self.assertEqual(calls["knowledge"], 0)
        self.assertEqual(calls["model"], 0)
        self.assertEqual(calls["catalog"], 0)
        self.assertEqual(calls["live"], 0)

    def test_neither_the_recycle_guard_nor_the_fallback_participate(self):
        _, calls, _ = self._turn("hola que tal", SHOWROOM_HISTORY)
        self.assertEqual(calls["recycle_guard"], 0)
        self.assertEqual(calls["fallback"], 0)

    def test_the_turn_is_logged_as_resolved_without_a_model(self):
        _, _, output = self._turn("hola que tal", SHOWROOM_HISTORY)
        self.assertIn("Mensaje social resuelto sin modelo", output)

    def test_every_social_wording_behaves_the_same(self):
        for text in ("hola", "buenas", "gracias", "ok", "perfecto",
                     "hola fred", "buenas tardes", "muchas gracias", "dale"):
            with self.subTest(text=text):
                reply, calls, _ = self._turn(text, SHOWROOM_HISTORY)
                self.assertTrue(reply)
                self.assertNotIn("showroom", reply.lower())
                self.assertEqual(calls["model"], 0)
                self.assertEqual(calls["knowledge"], 0)

    def test_a_real_question_still_runs_the_pipeline(self):
        """The shortcut must not swallow turns that need an answer."""
        _, calls, _ = self._turn("hacen envios a cordoba?", SHOWROOM_HISTORY)
        self.assertGreaterEqual(calls["knowledge"], 1)


class HistoryFallbackIsForEllipsisOnlyTests(unittest.TestCase):
    """Retrieval widens into history only for a turn that cannot stand alone."""

    def _retrieval(self, message, history):
        seen = []

        class _Bundle:
            def __init__(self, topic=""):
                self.governing_topic = topic
                self.context = ""

        def retriever(query):
            seen.append(query)
            # Whatever is asked WITH the showroom history attached comes back
            # as the showroom topic; the bare message matches nothing.
            return _Bundle("pickups_showroom" if "showroom" in query else "")

        retrieval, query, used_fallback = retrieve_with_recent_context(
            message, history, retriever,
            allow_history_fallback=not message_is_self_sufficient(message),
        )
        return retrieval.governing_topic, query, used_fallback, seen

    def test_a_greeting_does_not_inherit_the_previous_topic(self):
        topic, query, used_fallback, seen = self._retrieval(
            "hola que tal", SHOWROOM_HISTORY)
        self.assertEqual(topic, "")
        self.assertEqual(query, "hola que tal")
        self.assertFalse(used_fallback)
        self.assertEqual(seen, ["hola que tal"])

    def test_a_message_with_its_own_subject_does_not_either(self):
        topic, _, used_fallback, _ = self._retrieval(
            "cuanto sale el labial de moira", SHOWROOM_HISTORY)
        self.assertEqual(topic, "")
        self.assertFalse(used_fallback)

    def test_an_elliptical_turn_may_still_complete_from_history(self):
        topic, query, used_fallback, _ = self._retrieval(
            "quiero 4", SHOWROOM_HISTORY)
        self.assertTrue(used_fallback)
        self.assertIn("showroom", query)
        self.assertEqual(topic, "pickups_showroom")

    def test_the_default_still_allows_the_fallback(self):
        """The shadow harness calls this with three arguments."""

        class _Bundle:
            governing_topic = ""
            context = ""

        _, _, used_fallback = retrieve_with_recent_context(
            "quiero 4", SHOWROOM_HISTORY, lambda q: _Bundle())
        self.assertTrue(used_fallback)


class SelfSufficiencyTests(unittest.TestCase):
    """The predicate on its own."""

    def test_social_and_topical_messages_stand_alone(self):
        for text in ("hola", "hola que tal", "buenas", "gracias", "ok",
                     "perfecto", "hacen envios?", "pedido 6345",
                     "quiero pasar por el showroom",
                     "quiero informacion de mi pedido"):
            with self.subTest(text=text):
                self.assertTrue(message_is_self_sufficient(text))

    def test_elliptical_messages_do_not(self):
        for text in ("quiero 4", "y el precio?", "esas mismas", "2",
                     "las 4 por favor", "tambien lo quiero", "dame 2 mas"):
            with self.subTest(text=text):
                self.assertFalse(message_is_self_sufficient(text))


if __name__ == "__main__":
    unittest.main()
