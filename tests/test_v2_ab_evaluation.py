import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "bot", ROOT / "evals"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from run_fred_v2_ab import (  # noqa: E402
    CASES_PATH,
    fixture_order,
    fixture_product,
    hallucination_flags,
    score,
)
from v2_agent import FredV2Agent  # noqa: E402
from v2_shadow import ALLOWED_SHADOW_LOG_FIELDS, propose_shadow_turn  # noqa: E402
from v2_tools import V2ToolAdapters  # noqa: E402


class ABCaseBatteryTests(unittest.TestCase):
    def test_battery_has_at_least_fifty_unique_conversational_cases(self):
        import json

        cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(cases), 50)
        self.assertEqual(len(cases), len({case["id"] for case in cases}))
        self.assertGreaterEqual(sum(bool(case.get("history")) for case in cases), 15)
        self.assertTrue({
            "social", "topic_change", "order", "policy", "product", "purchase",
            "advice", "wholesale", "ambiguity",
        }.issubset({case["category"] for case in cases}))

    def test_order_fixture_preserves_fulfillment_truth(self):
        self.assertEqual("PACKED", fixture_order("6342")["fulfillment_status"])
        self.assertEqual("pickup", fixture_order("6342")["shipping_type"])
        self.assertEqual("TRK6343", fixture_order("6343")["tracking"])

    def test_product_fixture_has_real_identity_sku_stock_and_price_shape(self):
        result = fixture_product("Isabel I Chocolate")
        variant = result["products"][0]["variants"][0]
        self.assertEqual("ISABEL-I-CHOCOLATE", variant["sku"])
        self.assertEqual("in_stock", variant["status"])
        self.assertEqual("12500.00", variant["price"])
        self.assertFalse(fixture_product("Modelo Fantasma Ultra")["found"])


class ABScoringTests(unittest.TestCase):
    def test_wrong_tool_is_blocking(self):
        case = {"category": "policy", "expect": {"tools": ["search_knowledge"]}}
        result = {"reply": "hola", "tool_calls": [], "tool_results": [], "errors": []}
        evaluation = score(case, result)
        self.assertEqual("FAIL", evaluation["status"])
        self.assertIn("wrong_tool", evaluation["causes"])

    def test_unverified_commercial_fact_is_hallucination(self):
        result = {"reply": "Sale 12500", "tool_calls": [], "tool_results": []}
        self.assertIn("unsupported_fact:12500", hallucination_flags(result))

    def test_checkout_is_always_hallucination_blocker(self):
        result = {"reply": "Te creo el checkout", "tool_calls": [], "tool_results": []}
        self.assertIn("checkout", hallucination_flags(result))

    def test_packed_pickup_cannot_be_called_ready(self):
        result = {
            "reply": "Está listo para retirar.",
            "tool_calls": [{"name": "get_order", "arguments": {"order_number": "6342"}}],
            "tool_results": [{"name": "get_order", "result": fixture_order("6342")}],
        }
        self.assertIn("order_stage_overclaim:PACKED", hallucination_flags(result))

    def test_dry_run_handoff_cannot_be_claimed_as_sent(self):
        result = {
            "reply": "Te paso con Isa, te atiende enseguida.",
            "tool_calls": [{"name": "handoff_to_isa", "arguments": {}}],
            "tool_results": [{"name": "handoff_to_isa", "result": {"mode": "dry_run"}}],
        }
        self.assertIn("dry_run_handoff_claim", hallucination_flags(result))


class ShadowSafetyTests(unittest.TestCase):
    def test_production_webhook_does_not_import_v2(self):
        source = (ROOT / "bot" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("v2_agent", source)
        self.assertNotIn("v2_shadow", source)

    def test_shadow_returns_only_observation_fields_and_dry_run_handoff(self):
        responses = iter((
            {
                "content": None,
                "tool_calls": [{
                    "id": "handoff-1", "type": "function",
                    "function": {
                        "name": "handoff_to_isa",
                        "arguments": '{"reason":"purchase_intent","summary":"Quiere cuatro."}',
                    },
                }],
            },
            {"content": "Isa puede ayudarte con la compra."},
        ))
        tools = V2ToolAdapters(handoff=lambda payload: {"mode": "dry_run", **payload})
        agent = FredV2Agent(model_call=lambda messages: next(responses), tools=tools)
        record = propose_shadow_turn("quiero cuatro", agent=agent)
        self.assertEqual(ALLOWED_SHADOW_LOG_FIELDS, set(record))
        self.assertEqual("handoff_to_isa", record["decision"]["action"])
        self.assertNotIn("tool_results", record)
        self.assertNotIn("customer_phone", record)


if __name__ == "__main__":
    unittest.main()
