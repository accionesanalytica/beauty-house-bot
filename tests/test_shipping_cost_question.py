"""Tests for the "Fred Lite" fix: a shipping-cost question about the active
product must ask for the postal code (never invent a shipping amount, never
be confused with a courier/order-tracking issue), and must not have its
answer discarded merely because the model also answered a product question
in the same turn.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import agent  # noqa: E402
from agent import _shipping_cost_requested  # noqa: E402
from decision_schema import validate_model_decision  # noqa: E402


class ShippingCostRequestedTests(unittest.TestCase):
    def test_detects_envio_with_and_without_accent(self):
        self.assertTrue(_shipping_cost_requested("cuánto sería el envío?"))
        self.assertTrue(_shipping_cost_requested("cuanto sale el envio a cordoba"))

    def test_plain_product_question_is_not_a_shipping_question(self):
        self.assertFalse(_shipping_cost_requested("cuánto cuestan las Isabel I?"))

    def test_order_word_alone_is_not_a_shipping_question(self):
        self.assertFalse(_shipping_cost_requested("¿cómo va mi pedido?"))


def decision_call(payload, call_id="d1"):
    return {"id": call_id, "type": "function", "function": {
        "name": "set_turn_decision", "arguments": json.dumps(payload, ensure_ascii=False),
    }}


def get_stock_call(call_id, sku):
    return {"id": call_id, "type": "function", "function": {
        "name": "get_stock", "arguments": json.dumps({"sku": sku}),
    }}


class ShippingCostReplyIntegrationTests(unittest.TestCase):
    """Reproduces the reported real-world bug: customer asks about price AND
    shipping for the product they're already discussing ("esa opción"), and
    Fred answered by asking for an order number instead. Root cause fixed
    upstream (knowledge_rag's order_tracking dynamic requirement now needs
    real tracking evidence, not just "envío"); this covers the other half --
    once the model correctly resolves the product, the shipping question
    must still get an honest answer (ask for CP) instead of being silently
    dropped by the deterministic renderer."""

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_exact_match_with_price_and_shipping_asks_for_postal_code(self, ask_model, run_tool):
        decision = {
            "action": "reply", "reason": "normal_response",
            "summary": "Consultó precio y envío de Isabel I.",
            "response_mode": "product_discovery", "match_type": "exact_match",
            "requested_product": "esa opción", "matched_product": "SHOOW TOOLS - ISABEL I",
            "requested_product_type": "pestañas", "matched_product_type": "pestañas",
            "required_checks": ["live_price"],
        }
        ask_model.side_effect = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [get_stock_call("g1", "ISABEL-1"), decision_call(decision)],
            },
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)} if name == "set_turn_decision"
            else {"found": True, "sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I",
                  "status": "in_stock", "price": "30000"}
        )
        result = agent.answer(
            "cuanto cuestan, cuanto fuera el envio si elijo esa opcion? me puedes dar mas info?",
            history=[
                {"role": "user", "content": "Hola! Tengo dudas sobre las pestañas Shoow Isabel I"},
                {"role": "assistant", "content": "¡Hola! Sí, encontré SHOOW TOOLS - ISABEL I."},
            ],
        )

        reply = result["reply"]
        self.assertIn("30.000", reply)
        self.assertIn("código postal", reply)
        self.assertNotIn("número de orden", reply)

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_shipping_question_without_price_is_not_forced_into_the_clause(self, ask_model, run_tool):
        decision = {
            "action": "reply", "reason": "normal_response", "summary": "Sólo pidió info.",
            "response_mode": "product_discovery", "match_type": "exact_match",
            "requested_product": "Isabel I", "matched_product": "SHOOW TOOLS - ISABEL I",
            "requested_product_type": "pestañas", "matched_product_type": "pestañas",
            "required_checks": [],
        }
        ask_model.side_effect = [
            {"content": "", "_fred_usage": {}, "tool_calls": [decision_call(decision)]},
            {"content": "", "_fred_usage": {}},
        ]
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)} if name == "set_turn_decision" else []
        )
        result = agent.answer("dame más info de Isabel I", history=[])

        self.assertNotIn("código postal", result["reply"])


if __name__ == "__main__":
    unittest.main()
