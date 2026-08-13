"""Tests for the graceful, 3-tier discovery fallback that replaces both the
proactive convergence nudge and the single-tier M2A fallback.

Principle: if Fred cannot close a single confident recommendation, degrade
calmly instead of forcing convergence or defaulting to a rigid "necesito el
modelo exacto o link" on every discovery turn.

  Tier "single"   — exactly one verified candidate, every required check
                    already satisfied by a real tool -> recommend it.
  Tier "multi"    — 2-3 verified candidates -> show a short menu.
  Tier "escalate" — 0, >3, or one unverifiable candidate -> offer to explore
                    options or talk to Isa (an offer in text, nothing
                    executed).
  Tier "none"     — not a discovery turn, or a handoff is already required;
                    this fallback does not apply at all.

Everything here is offline (DeepSeek and tools mocked); nothing calls a real
API.
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
    _collect_turn_candidates,
    _filter_relevant_candidates,
    classify_graceful_discovery_fallback,
)
from decision_schema import validate_model_decision  # noqa: E402


ONE_SEARCH_RESULT = [{
    "product_id": 1, "name": "SHOOW TOOLS - NATURAL SHOOW",
    "variants": [{"sku": "NATURAL-1", "description": "10 pares", "quantity": 8}],
}]
THREE_SEARCH_RESULTS = [
    {"product_id": 1, "name": "SHOOW TOOLS - NATURAL SHOOW", "variants": [{"sku": "NATURAL-1", "quantity": 8}]},
    {"product_id": 2, "name": "SHOOW TOOLS - ISABEL I", "variants": [{"sku": "ISABEL-1", "quantity": 3}]},
    {"product_id": 3, "name": "SHOOW TOOLS - FOXY #1", "variants": [{"sku": "FOXY-1", "quantity": 5}]},
]


class CollectTurnCandidatesTests(unittest.TestCase):
    def test_dedupes_the_same_product_seen_from_search_and_get_stock(self):
        facts = {"natural-1": {"sku": "NATURAL-1", "product_name": "SHOOW TOOLS - NATURAL SHOOW",
                                "status": "in_stock", "price": "36000"}}
        candidates = _collect_turn_candidates(ONE_SEARCH_RESULT, facts)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["price"], "36000")
        self.assertEqual(candidates[0]["status"], "in_stock")

    def test_search_only_candidate_has_no_price_but_has_verified_stock(self):
        candidates = _collect_turn_candidates(ONE_SEARCH_RESULT, {})
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0]["price"])
        # search_available_products only ever returns positive-stock variants:
        # this "in_stock" is a real guarantee from the tool, not inferred here.
        self.assertEqual(candidates[0]["status"], "in_stock")

    def test_three_distinct_products_stay_distinct(self):
        candidates = _collect_turn_candidates(THREE_SEARCH_RESULTS, {})
        self.assertEqual(len(candidates), 3)

    def test_get_product_availability_out_of_stock_is_never_shown_as_available(self):
        facts = {"x": {"sku": "X-1", "product_name": "Producto X", "status": "out_of_stock", "price": None}}
        candidates = _collect_turn_candidates([], facts)
        self.assertEqual(candidates[0]["status"], "out_of_stock")

    def test_empty_inputs_yield_no_candidates(self):
        self.assertEqual(_collect_turn_candidates([], {}), [])


class ClassifyGracefulDiscoveryFallbackTests(unittest.TestCase):
    def test_not_a_discovery_turn_is_none_even_with_one_perfect_candidate(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=False, handoff_request=None,
            candidates=[{"sku": "X"}], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "none")

    def test_handoff_present_is_none_regardless_of_candidates(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request={"reason": "unable_to_verify"},
            candidates=[{"sku": "X"}], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "none")

    def test_single_candidate_with_checks_satisfied_is_single(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": "NATURAL-1"}],
            required_checks={"live_price"}, checks_completed={"live_price", "live_stock"},
        )
        self.assertEqual(tier, "single")

    def test_single_candidate_with_pending_check_degrades_to_escalate_not_rigid_gate(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": "NATURAL-1"}],
            required_checks={"live_price"}, checks_completed={"live_stock"},
        )
        self.assertEqual(tier, "escalate")

    def test_single_candidate_without_sku_is_escalate(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": ""}], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "escalate")

    def test_two_candidates_is_multi(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": "A"}, {"sku": "B"}], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "multi")

    def test_three_candidates_is_multi(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": "A"}, {"sku": "B"}, {"sku": "C"}], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "multi")

    def test_four_candidates_is_escalate_not_multi(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[{"sku": c} for c in "ABCD"], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "escalate")

    def test_zero_candidates_is_escalate(self):
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=[], required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(tier, "escalate")


class FilterRelevantCandidatesTests(unittest.TestCase):
    """Candidate-relevance audit follow-up: a real, in-stock accessory
    (glue, tweezers, curlers, ...) should never be offered as the primary
    discovery candidate for a product search, unless the customer's own
    message names that accessory type. This is a category-level keyword
    filter, not a per-product/per-SKU one — mirrors the existing "sorpresa"
    exclusion already in tiendanube_tools.search_available_products()."""

    GLUE = {"product_name": "AOA STUDIO - PEGA DE PESTAÑAS COREANA", "sku": "3D24A", "price": None, "status": "in_stock"}
    LASHES_10_PAR = {"product_name": "PESTAÑAS 10 PARES CRUZADAS", "sku": "P10-1", "price": None, "status": "in_stock"}
    NATURAL_SHOOW = {"product_name": "SHOOW TOOLS - NATURAL SHOOW", "sku": "NATURAL-1", "price": None, "status": "in_stock"}

    def test_accessory_candidate_is_excluded_when_not_requested(self):
        result = _filter_relevant_candidates(
            [self.GLUE, self.LASHES_10_PAR], "quiero pestañas, algo natural",
        )
        names = [c["product_name"] for c in result]
        self.assertNotIn(self.GLUE["product_name"], names)
        self.assertIn(self.LASHES_10_PAR["product_name"], names)

    def test_accessory_candidate_is_kept_when_customer_explicitly_asks_for_it(self):
        result = _filter_relevant_candidates(
            [self.GLUE, self.LASHES_10_PAR], "necesito pegamento para pestañas",
        )
        names = [c["product_name"] for c in result]
        self.assertIn(self.GLUE["product_name"], names)
        self.assertIn(self.LASHES_10_PAR["product_name"], names)

    def test_real_lash_products_are_never_filtered_regardless_of_naming(self):
        candidates = [self.LASHES_10_PAR, self.NATURAL_SHOOW]
        result = _filter_relevant_candidates(candidates, "quiero pestañas, algo natural")
        self.assertEqual(result, candidates)

    def test_filtering_down_to_one_candidate_reclassifies_as_single(self):
        candidates = _filter_relevant_candidates(
            [self.GLUE, self.LASHES_10_PAR], "quiero pestañas, algo natural",
        )
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=candidates, required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(tier, "single")

    def test_filtering_down_to_zero_candidates_is_escalate_not_empty_multi(self):
        candidates = _filter_relevant_candidates([self.GLUE], "quiero pestañas, algo natural")
        tier = classify_graceful_discovery_fallback(
            product_discovery_turn=True, handoff_request=None,
            candidates=candidates, required_checks=set(), checks_completed=set(),
        )
        self.assertEqual(candidates, [])
        self.assertEqual(tier, "escalate")

    def test_does_not_mutate_the_input_list_or_its_dicts(self):
        candidates = [dict(self.GLUE), dict(self.LASHES_10_PAR)]
        original = [dict(c) for c in candidates]
        _filter_relevant_candidates(candidates, "quiero pestañas, algo natural")
        self.assertEqual(candidates, original)


def search_call(call_id, query):
    return {"id": call_id, "type": "function", "function": {
        "name": "search_available_products", "arguments": json.dumps({"query": query}),
    }}


def get_stock_call(call_id, sku):
    return {"id": call_id, "type": "function", "function": {
        "name": "get_stock", "arguments": json.dumps({"sku": sku}),
    }}


def decision_call(payload, call_id="d1"):
    return {"id": call_id, "type": "function", "function": {
        "name": "set_turn_decision", "arguments": json.dumps(payload, ensure_ascii=False),
    }}


class GracefulFallbackIntegrationTests(unittest.TestCase):
    """End-to-end with agent.answer(), model/tools mocked."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_never_closes_decision_one_verified_candidate_tier_single(self, ask_model, run_tool):
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "pestañas natural")]},
            {"content": "", "_fred_usage": {}, "tool_calls": [get_stock_call("g1", "NATURAL-1")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "search_available_products":
                return ONE_SEARCH_RESULT
            if name == "get_stock":
                return {"found": True, "sku": "NATURAL-1", "product_name": "SHOOW TOOLS - NATURAL SHOOW",
                        "status": "in_stock", "price": "36000"}
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, cuanto sale?", history=[])

        self.assertEqual(result.get("graceful_fallback_tier"), "single")
        self.assertIn("Encontré SHOOW TOOLS - NATURAL SHOOW", result["reply"])
        self.assertIn("36.000", result["reply"])
        self.assertNotIn("Esto es exactamente lo que buscás", result["reply"])
        self.assertEqual(result["decision"]["match_type"], "unclassified")
        self.assertNotEqual(result["decision"]["match_type"], "exact_match")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_never_closes_decision_three_candidates_tier_multi_no_invention(self, ask_model, run_tool):
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "pestañas")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            THREE_SEARCH_RESULTS if name == "search_available_products" else []
        )
        result = agent.answer("Quiero pestañas, algo natural", history=[])

        self.assertEqual(result.get("graceful_fallback_tier"), "multi")
        reply = result["reply"]
        self.assertIn("SHOOW TOOLS - NATURAL SHOOW", reply)
        self.assertIn("SHOOW TOOLS - ISABEL I", reply)
        self.assertIn("SHOOW TOOLS - FOXY #1", reply)
        # None of these were priced by any tool: never invent a number.
        self.assertNotIn("$", reply)
        self.assertEqual(result["decision"]["match_type"], "multiple_candidates")
        self.assertNotEqual(result["decision"]["match_type"], "exact_match")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_multi_tier_shows_price_only_for_the_candidate_that_has_it(self, ask_model, run_tool):
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [search_call("s1", "pestañas"), get_stock_call("g1", "ISABEL-1")],
            },
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "search_available_products":
                return THREE_SEARCH_RESULTS
            if name == "get_stock":
                return {"found": True, "sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I",
                        "status": "in_stock", "price": "30000"}
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, cuanto sale?", history=[])

        self.assertEqual(result.get("graceful_fallback_tier"), "multi")
        reply = result["reply"]
        self.assertIn("30.000", reply)
        # Only ISABEL I had a verified price; the other two must not show one.
        natural_line = next(line for line in reply.splitlines() if "NATURAL SHOOW" in line)
        foxy_line = next(line for line in reply.splitlines() if "FOXY" in line)
        self.assertNotIn("$", natural_line)
        self.assertNotIn("$", foxy_line)

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_never_closes_decision_no_candidates_tier_escalate_offers_isa_without_executing(self, ask_model, run_tool):
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "algo raro")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: []  # nothing found, ever
        result = agent.answer("Quiero pestañas, algo natural", history=[])

        self.assertEqual(result.get("graceful_fallback_tier"), "escalate")
        self.assertIn("Isa", result["reply"])
        self.assertIsNone(result.get("handoff"))  # only offered in text, never executed
        self.assertIsNone(result.get("sale_candidate"))

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_handoff_already_required_keeps_priority_over_the_fallback(self, ask_model, run_tool):
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [
                    get_stock_call("g1", "NATURAL-1"),
                    {"id": "h1", "type": "function", "function": {
                        "name": "request_isa_handoff",
                        "arguments": json.dumps({"reason": "unable_to_verify", "summary": "no seguro"}),
                    }},
                ],
            },
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "get_stock":
                return {"found": True, "sku": "NATURAL-1", "product_name": "SHOOW TOOLS - NATURAL SHOOW",
                        "status": "in_stock", "price": "36000"}
            if name == "request_isa_handoff":
                return {"handoff_requested": True, "reason": arguments.get("reason")}
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, cuanto sale?", history=[])

        self.assertNotIn("graceful_fallback_tier", result)
        self.assertEqual(result["decision"]["action"], "handoff_to_isa")
        self.assertNotIn("36.000", result["reply"])

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_quick_lookup_that_closes_normally_never_reaches_the_fallback(self, ask_model, run_tool):
        decision = {
            "action": "reply", "reason": "normal_response", "summary": "Verificado.",
            "response_mode": "product_discovery", "match_type": "exact_match",
            "requested_product": "Taylor", "matched_product": "Taylor",
            "requested_product_type": "pestañas", "matched_product_type": "pestañas",
            "required_checks": ["live_price"],
        }
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [get_stock_call("g1", "TAYLOR-1"), decision_call(decision)],
            },
            {"content": "Sale $28.000.", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)} if name == "set_turn_decision"
            else {"found": True, "sku": "TAYLOR-1", "product_name": "Taylor", "status": "in_stock", "price": "28000"}
            if name == "get_stock" else []
        )
        result = agent.answer("¿Cuánto salen las Taylor?", history=[])

        self.assertNotIn("graceful_fallback_tier", result)
        self.assertEqual(result["decision"]["match_type"], "exact_match")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_glue_candidate_never_shown_as_primary_for_a_lash_search(self, ask_model, run_tool):
        """The observed real-world bug: a lash-glue result from
        search_available_products("pestañas") must not appear in the
        Tier 2 menu when the customer is asking about pestañas in
        general, but the two genuine lash candidates still should."""
        three_with_glue = [
            {"product_id": 1, "name": "AOA STUDIO - PEGA DE PESTAÑAS COREANA",
             "variants": [{"sku": "3D24A", "quantity": 4}]},
            {"product_id": 2, "name": "PESTAÑAS 10 PARES CRUZADAS",
             "variants": [{"sku": "P10-1", "quantity": 8}]},
            {"product_id": 3, "name": "SHOOW TOOLS - NATURAL SHOOW",
             "variants": [{"sku": "NATURAL-1", "quantity": 8}]},
        ]
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [search_call("s1", "pestañas")]},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            three_with_glue if name == "search_available_products" else []
        )
        result = agent.answer("quiero pestañas, algo natural", history=[])

        reply = result["reply"]
        self.assertNotIn("PEGA DE PESTAÑAS", reply)
        self.assertIn("PESTAÑAS 10 PARES CRUZADAS", reply)
        self.assertIn("SHOOW TOOLS - NATURAL SHOOW", reply)
        # Only 2 real candidates survive the filter -> still tier "multi".
        self.assertEqual(result.get("graceful_fallback_tier"), "multi")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_real_ambiguity_resolved_immediately_is_unaffected(self, ask_model, run_tool):
        decision = {
            "action": "reply", "reason": "normal_response", "summary": "No hay match.",
            "response_mode": "product_discovery", "match_type": "no_match",
            "requested_product": "Valentina Deluxe", "matched_product": "",
            "requested_product_type": "pestañas", "matched_product_type": "",
            "required_checks": [],
        }
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [decision_call(decision)]},
            {"content": "texto ignorado", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)} if name == "set_turn_decision" else []
        )
        result = agent.answer("¿Tenés las Valentina Deluxe?", history=[])

        self.assertNotIn("graceful_fallback_tier", result)
        self.assertIn("No encontré", result["reply"])
        self.assertEqual(result["decision"]["match_type"], "no_match")


if __name__ == "__main__":
    unittest.main()
