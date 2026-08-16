"""Similarity may inform an answer; it may never BE the answer.

The graceful discovery fallback degrades from whatever real tools verified
during the turn. That is sound as long as the turn had something to degrade
FROM. When the customer's message names and describes nothing -- "Las quiero",
"sí dale" -- the only thing connecting a candidate to the conversation is
vector similarity, and production showed exactly what that produces: a customer
who wrote "Las quiero" was offered silver hair flowers.

So the fallback now asks in that case. Not escalates (there is nothing wrong),
not recommends (there is nothing confirmed): asks.

Everything here is offline; DeepSeek and tools are mocked.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import agent  # noqa: E402
from agent import (  # noqa: E402
    _message_carries_product_content,
    _turn_has_a_product_anchor,
    classify_graceful_discovery_fallback,
)


UNRELATED_CANDIDATE = [{
    "product_id": 9, "name": "Flores plateadas para el pelo",
    "variants": [{"sku": "FLORES-1", "description": "Única", "quantity": 4}],
}]


def search_call(call_id, query):
    return {"id": call_id, "type": "function", "function": {
        "name": "search_available_products", "arguments": json.dumps({"query": query}),
    }}


class MessageProductContentTests(unittest.TestCase):
    def test_pure_reference_and_courtesy_carry_no_product_content(self):
        for message in (
            "Las quiero", "las quiero!", "sí dale", "dale", "esas",
            "quiero dos", "listo, perfecto", "hola fred",
        ):
            with self.subTest(message=message):
                self.assertFalse(_message_carries_product_content(message))

    def test_any_real_product_word_counts_as_content(self):
        for message in (
            "quiero comprar Isabel I", "busco pestañas naturales",
            "algo natural para todos los días", "tenés el modelo chocolate?",
        ):
            with self.subTest(message=message):
                self.assertTrue(_message_carries_product_content(message))

    def test_an_active_product_anchors_an_otherwise_empty_follow_up(self):
        # "Las quiero" is a perfectly good sentence once the conversation
        # already has a product. The anchor is the active product, not the
        # words -- so this must NOT be blocked.
        self.assertTrue(_turn_has_a_product_anchor(
            "Las quiero",
            "Producto activo de esta conversación: SHOOW TOOLS - ISABEL I.",
        ))

    def test_without_an_active_product_an_empty_follow_up_has_no_anchor(self):
        self.assertFalse(_turn_has_a_product_anchor("Las quiero", "Candidatas del catálogo: ..."))


class AnchorClassificationTests(unittest.TestCase):
    """The anchor is checked before the candidate count, on purpose."""

    def test_one_perfect_candidate_without_an_anchor_is_ask_not_single(self):
        # The dangerous case: a single similarity hit reads as a confident
        # answer. One is not safer than three here -- it is worse.
        self.assertEqual(
            classify_graceful_discovery_fallback(
                product_discovery_turn=True, handoff_request=None,
                candidates=[{"product_name": "Flores plateadas", "sku": "FLORES-1"}],
                has_product_anchor=False,
            ),
            "ask",
        )

    def test_several_candidates_without_an_anchor_are_ask_not_multi(self):
        self.assertEqual(
            classify_graceful_discovery_fallback(
                product_discovery_turn=True, handoff_request=None,
                candidates=[
                    {"product_name": "A", "sku": "A1"},
                    {"product_name": "B", "sku": "B1"},
                ],
                has_product_anchor=False,
            ),
            "ask",
        )

    def test_an_anchored_turn_keeps_every_existing_tier(self):
        one = [{"product_name": "SHOOW TOOLS - NATURAL SHOOW", "sku": "NATURAL-1"}]
        two = one + [{"product_name": "SHOOW TOOLS - FOXY #1", "sku": "FOXY-1"}]
        for candidates, expected in ((one, "single"), (two, "multi"), ([], "escalate")):
            with self.subTest(expected=expected):
                self.assertEqual(
                    classify_graceful_discovery_fallback(
                        product_discovery_turn=True, handoff_request=None,
                        candidates=candidates, has_product_anchor=True,
                    ),
                    expected,
                )

    def test_a_required_handoff_still_outranks_the_anchor_check(self):
        self.assertEqual(
            classify_graceful_discovery_fallback(
                product_discovery_turn=True,
                handoff_request={"reason": "human_request", "summary": "x"},
                candidates=[], has_product_anchor=False,
            ),
            "none",
        )

    def test_a_non_discovery_turn_is_untouched(self):
        self.assertEqual(
            classify_graceful_discovery_fallback(
                product_discovery_turn=False, handoff_request=None,
                candidates=[], has_product_anchor=False,
            ),
            "none",
        )


class AnchorEndToEndTests(unittest.TestCase):
    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_las_quiero_never_gets_offered_a_similarity_candidate(self, ask_model, run_tool):
        """The production bug, reproduced: the retrieval context makes this a
        discovery turn, the tool returns an unrelated product, and the model
        never closes a decision. Fred must ask, not recommend."""
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "flores")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            UNRELATED_CANDIDATE if name == "search_available_products" else []
        )

        result = agent.answer(
            "Las quiero",
            history=[],
            rag_context="Candidatas del catálogo: Flores plateadas para el pelo",
        )

        self.assertEqual(result.get("graceful_fallback_tier"), "ask")
        reply = result["reply"]
        self.assertNotIn("Flores", reply)
        self.assertNotIn("Encontré", reply)
        self.assertIn("?", reply)
        # It must not silently escalate either: asking is the answer.
        self.assertIsNone(result.get("handoff"))
        self.assertIsNone(result.get("sale_candidate"))
        self.assertEqual(result["decision"]["match_type"], "unresolved")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_the_same_turn_with_an_active_product_is_allowed_to_answer(self, ask_model, run_tool):
        """Same empty words, but the conversation already has a product. The
        anchor exists, so the existing behaviour is preserved -- this change
        must not break legitimate elliptical follow-ups."""
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "isabel")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: ([{
            "product_id": 1, "name": "SHOOW TOOLS - ISABEL I",
            "variants": [{"sku": "ISABEL-1", "description": "8/10 mm", "quantity": 3}],
        }] if name == "search_available_products" else [])

        result = agent.answer(
            "Las quiero",
            history=[],
            rag_context=(
                "Producto activo de esta conversación: SHOOW TOOLS - ISABEL I.\n\n"
                "Candidatas del catálogo: SHOOW TOOLS - ISABEL I"
            ),
        )

        self.assertEqual(result.get("graceful_fallback_tier"), "single")
        self.assertIn("ISABEL I", result["reply"])


if __name__ == "__main__":
    unittest.main()
