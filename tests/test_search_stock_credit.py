"""Tests for crediting search_available_products as a real live_stock check,
and for the existing auto-fetch mechanism completing live_price afterwards.

Root cause (see the D1.1 production audit): search_available_products is a
genuine live Tiendanube stock check, but until now nothing in agent.py ever
recorded that — only get_stock/get_product_availability updated
checks_completed/commercial_facts_by_sku. A discovery decision built only
from search_available_products always had checks_completed=[], so
routing_policy's grounded-discovery bypass could never fire for it even when
the underlying stock claim was genuinely live-verified.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import agent  # noqa: E402
from decision_schema import validate_model_decision  # noqa: E402
from agent import _credit_verified_stock_from_search, _select_fact_for_auto_fetch  # noqa: E402


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


DISCOVERY_DECISION = {
    "action": "reply", "reason": "normal_response",
    "summary": "Encontré pestañas naturales con stock confirmado por búsqueda.",
    "response_mode": "product_discovery", "match_type": "exact_match",
    "requested_product": "pestañas naturales", "matched_product": "SHOOW TOOLS - NATURAL SHOOW",
    "requested_product_type": "pestañas", "matched_product_type": "pestañas",
    "required_checks": ["live_price"],
}

SEARCH_RESULT = [{
    "product_id": 1, "name": "SHOOW TOOLS - NATURAL SHOOW",
    "variants": [{"sku": "NATURAL-1", "description": "10 pares", "quantity": 8}],
}]


class CreditVerifiedStockFromSearchTests(unittest.TestCase):
    """Pure unit tests of the new helper — no model, no agent loop."""

    def test_credits_live_stock_only_never_live_price(self):
        checks_completed = set()
        facts = {}
        _credit_verified_stock_from_search(
            "SHOOW TOOLS - NATURAL SHOOW", SEARCH_RESULT, facts, checks_completed
        )
        self.assertEqual(checks_completed, {"live_stock"})
        self.assertNotIn("live_price", checks_completed)

    def test_credited_fact_has_no_invented_price(self):
        checks_completed = set()
        facts = {}
        _credit_verified_stock_from_search(
            "SHOOW TOOLS - NATURAL SHOOW", SEARCH_RESULT, facts, checks_completed
        )
        fact = facts["natural-1"]
        self.assertEqual(fact["status"], "in_stock")
        self.assertIsNone(fact["price"])
        self.assertEqual(fact["sku"], "NATURAL-1")

    def test_ignores_products_that_do_not_match_the_declared_match(self):
        checks_completed = set()
        facts = {}
        _credit_verified_stock_from_search(
            "Isabel I", SEARCH_RESULT, facts, checks_completed
        )
        self.assertEqual(checks_completed, set())
        self.assertEqual(facts, {})

    def test_never_overwrites_a_price_already_known_from_get_stock(self):
        checks_completed = {"live_price"}
        facts = {"natural-1": {"sku": "NATURAL-1", "price": "36000", "status": "in_stock"}}
        _credit_verified_stock_from_search(
            "SHOOW TOOLS - NATURAL SHOOW", SEARCH_RESULT, facts, checks_completed
        )
        self.assertEqual(facts["natural-1"]["price"], "36000")


class SearchOnlyDiscoveryTests(unittest.TestCase):
    """1. discovery + pide precio + sólo search.

    The existing auto-fetch safety net (agent.py lines ~600-630) means a
    *complete* turn actually tries get_stock automatically once search
    credits exactly one commercial fact — see AutoFetchAfterSearchTests for
    the case where that succeeds. This class isolates the case where the
    automatic attempt still cannot verify a price, which must never be
    invented regardless."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_price_never_invented_when_auto_fetch_cannot_verify_it(self, ask_model, run_tool):
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [search_call("s1", "pestañas natural"), decision_call(DISCOVERY_DECISION)],
            },
            # Round after search+decision: the orchestrator notices live_price
            # is still missing and exactly one fact is known, so it tries
            # get_stock automatically here (no extra model call needed for
            # that attempt itself) and loops once more for the final text.
            {"content": "", "_fred_usage": {}},
            {"content": "texto final", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "search_available_products":
                return SEARCH_RESULT
            if name == "get_stock":
                return {"found": False, "sku": arguments.get("sku"), "message": "No encontré ese código."}
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, cuanto sale?", history=[])

        self.assertNotIn("$", result["reply"])
        self.assertNotIn("live_price", result["decision"].get("checks_completed") or [])
        self.assertIn("no pude confirmar el precio", result["reply"].lower())


class AutoFetchAfterSearchTests(unittest.TestCase):
    """2. discovery + pide precio + search + get_stock (via the existing
    auto-fetch safety net, now able to find a SKU because search credited
    exactly one commercial fact)."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_auto_fetch_completes_live_price_and_allows_a_verified_price(self, ask_model, run_tool):
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [search_call("s1", "pestañas natural"), decision_call(DISCOVERY_DECISION)],
            },
            # This round has empty tool_calls: the orchestrator itself runs
            # get_stock automatically here (missing_checks + one known fact)
            # and loops once more instead of consuming another model round.
            {"content": "", "_fred_usage": {}},
            {"content": "texto final ignorado", "_fred_usage": {}},
        ]
        get_stock_calls = []

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "search_available_products":
                return SEARCH_RESULT
            if name == "get_stock":
                get_stock_calls.append(arguments)
                return {
                    "found": True, "sku": "NATURAL-1", "product_name": "SHOOW TOOLS - NATURAL SHOOW",
                    "variant": "10 pares", "status": "in_stock", "quantity": 8, "price": "36000",
                }
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, cuanto sale?", history=[])

        self.assertEqual(get_stock_calls, [{"sku": "NATURAL-1"}])
        decision = result["decision"]
        self.assertEqual(set(decision["required_checks"]), {"live_price"})
        self.assertIn("live_price", decision["checks_completed"])
        self.assertIn("live_stock", decision["checks_completed"])
        self.assertIn("36.000", result["reply"])


class StockOnlyDiscoveryTests(unittest.TestCase):
    """3. discovery + pide stock + search: grounded without needing get_stock,
    because search_available_products already proves live stock."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_stock_question_is_satisfied_by_search_alone(self, ask_model, run_tool):
        stock_decision = {**DISCOVERY_DECISION, "required_checks": ["live_stock"]}
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [search_call("s1", "pestañas natural"), decision_call(stock_decision)],
            },
            {"content": "texto final ignorado", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "search_available_products":
                return SEARCH_RESULT
            self.fail("get_stock no debería hacer falta: live_stock ya lo prueba la búsqueda")

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural, ¿tienen stock?", history=[])

        decision = result["decision"]
        self.assertEqual(set(decision["required_checks"]), {"live_stock"})
        self.assertEqual(decision["checks_completed"], ["live_stock"])
        self.assertIn("Está disponible", result["reply"])
        self.assertNotIn("$", result["reply"])


class DiscoveryWithoutChecksTests(unittest.TestCase):
    """4. discovery sin precio/stock pedido: no exige ningún check."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_pure_recommendation_needs_no_live_check(self, ask_model, run_tool):
        plain_decision = {
            **DISCOVERY_DECISION,
            "summary": "Sólo pidió una recomendación, sin precio ni stock.",
            "required_checks": [],
        }
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [search_call("s1", "pestañas natural"), decision_call(plain_decision)],
            },
            {"content": "texto final ignorado", "_fred_usage": {}},
        ]

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "search_available_products":
                return SEARCH_RESULT
            self.fail("no debería llamar ninguna otra herramienta: nada se pidió verificar")

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas, algo natural", history=[])

        decision = result["decision"]
        self.assertEqual(decision["required_checks"], [])
        self.assertIn("SHOOW TOOLS - NATURAL SHOOW", result["reply"])


THREE_CANDIDATES = {
    "natural-1": {"sku": "NATURAL-1", "product_name": "SHOOW TOOLS - NATURAL SHOOW", "status": "in_stock", "price": None},
    "isabel-1": {"sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I", "status": "in_stock", "price": None},
    "foxy-1": {"sku": "FOXY-1", "product_name": "SHOOW TOOLS - FOXY #1", "status": "in_stock", "price": None},
}


class SelectFactForAutoFetchTests(unittest.TestCase):
    """Pure tests of the multi-candidate SKU selector — no model, no agent loop."""

    def test_single_known_candidate_is_used_unchanged(self):
        one = {"natural-1": THREE_CANDIDATES["natural-1"]}
        fact = _select_fact_for_auto_fetch({"matched_product": "cualquier cosa"}, one)
        self.assertEqual(fact["sku"], "NATURAL-1")

    def test_three_candidates_matched_product_identifies_exactly_one(self):
        decision = {"matched_product": "SHOOW TOOLS - ISABEL I"}
        fact = _select_fact_for_auto_fetch(decision, THREE_CANDIDATES)
        self.assertIsNotNone(fact)
        self.assertEqual(fact["sku"], "ISABEL-1")

    def test_three_candidates_ambiguous_name_refuses_to_guess(self):
        # "SHOOW TOOLS" alone substring-matches all three product names.
        decision = {"matched_product": "SHOOW TOOLS"}
        fact = _select_fact_for_auto_fetch(decision, THREE_CANDIDATES)
        self.assertIsNone(fact)

    def test_matched_product_absent_among_facts_refuses_to_guess(self):
        decision = {"matched_product": "Taylor"}  # not among the three candidates
        fact = _select_fact_for_auto_fetch(decision, THREE_CANDIDATES)
        self.assertIsNone(fact)

    def test_no_matched_product_at_all_with_multiple_candidates_refuses(self):
        fact = _select_fact_for_auto_fetch({}, THREE_CANDIDATES)
        self.assertIsNone(fact)


class MultiCandidateAutoFetchIntegrationTests(unittest.TestCase):
    """End-to-end: the model explores three products via get_product_
    availability before deciding — required_checks/checks_completed and the
    auto-fetch must resolve exactly the chosen candidate, never a guess."""

    def _availability_call(self, call_id, product_id):
        return {"id": call_id, "type": "function", "function": {
            "name": "get_product_availability", "arguments": json.dumps({"product_id": product_id}),
        }}

    def _availability_result(self, sku, product_name):
        return {
            "found": True, "product_name": product_name,
            "variants": [{"sku": sku, "variant": "10 pares", "status": "in_stock", "quantity": 5}],
        }

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_auto_fetch_targets_only_the_matched_candidate_among_three(self, ask_model, run_tool):
        decision = {
            **DISCOVERY_DECISION,
            "matched_product": "SHOOW TOOLS - ISABEL I",
        }
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [
                    self._availability_call("a1", 1),
                    self._availability_call("a2", 2),
                    self._availability_call("a3", 3),
                    decision_call(decision),
                ],
            },
            {"content": "", "_fred_usage": {}},  # triggers the auto-fetch round
            {"content": "texto final", "_fred_usage": {}},
        ]
        get_stock_calls = []
        availability_by_id = {
            1: self._availability_result("NATURAL-1", "SHOOW TOOLS - NATURAL SHOOW"),
            2: self._availability_result("ISABEL-1", "SHOOW TOOLS - ISABEL I"),
            3: self._availability_result("FOXY-1", "SHOOW TOOLS - FOXY #1"),
        }

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "get_product_availability":
                return availability_by_id[arguments["product_id"]]
            if name == "get_stock":
                get_stock_calls.append(arguments)
                return {
                    "found": True, "sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I",
                    "variant": "10 pares", "status": "in_stock", "quantity": 5, "price": "30000",
                }
            return []

        run_tool.side_effect = run_tool_side_effect
        result = agent.answer("Quiero pestañas Isabel, cuanto sale?", history=[])

        # 4. exactly one get_stock call, and only for the matched candidate.
        self.assertEqual(get_stock_calls, [{"sku": "ISABEL-1"}])
        # 5. never priced a different explored candidate.
        self.assertNotIn("NATURAL-1", str(get_stock_calls))
        self.assertNotIn("FOXY-1", str(get_stock_calls))
        self.assertIn("30.000", result["reply"])
        decision_out = result["decision"]
        self.assertIn("live_price", decision_out["checks_completed"])
        self.assertEqual(decision_out.get("matched_product"), "SHOOW TOOLS - ISABEL I")


if __name__ == "__main__":
    unittest.main()
