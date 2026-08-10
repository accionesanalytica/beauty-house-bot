"""Offline tests for the structured decision boundary."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from decision_schema import build_effective_decision, validate_model_decision  # noqa: E402
from tool_guardrails import bounded_customer_reply, validate_tool_arguments  # noqa: E402
import agent  # noqa: E402


class DecisionSchemaTests(unittest.TestCase):
    def test_valid_normal_reply_is_accepted(self):
        decision = validate_model_decision({
            "action": "reply", "reason": "normal_response", "summary": "Orientó a la clienta.",
        })
        self.assertEqual(decision["action"], "reply")

    def test_malformed_or_unknown_model_decision_is_rejected(self):
        self.assertIsNone(validate_model_decision({"action": "create_checkout"}))
        self.assertIsNone(validate_model_decision({
            "action": "handoff_to_isa", "reason": "anything", "summary": "x",
        }))

    def test_sale_intake_requires_verified_candidate_not_model_claim(self):
        result = build_effective_decision({
            "action": "start_sales_intake", "reason": "purchase_intent", "summary": "La clienta compra.",
        })
        self.assertEqual(result["action"], "reply")

        result = build_effective_decision(
            None,
            sale_candidate={"sku": "ISABEL-1", "product_name": "Isabel I"},
        )
        self.assertEqual(result["action"], "start_sales_intake")

    def test_handoff_requires_existing_handoff_fact(self):
        result = build_effective_decision({
            "action": "handoff_to_isa", "reason": "human_request", "summary": "La pasa a Isa.",
        })
        self.assertEqual(result["action"], "reply")

        result = build_effective_decision(
            None, handoff={"reason": "human_request", "summary": "Pidió hablar con Isa."}
        )
        self.assertEqual(result["action"], "handoff_to_isa")

    def test_clarification_is_deterministic_after_tool_limit(self):
        result = build_effective_decision(None, needs_product_clarification=True)
        self.assertEqual(result["action"], "clarify_product")

    def test_tool_arguments_are_bounded_and_unknown_tools_are_rejected(self):
        arguments, error = validate_tool_arguments(
            "search_products", {"query": "Isabel I", "limit": 99}
        )
        self.assertIsNone(arguments)
        self.assertIn("Búsqueda inválida", error)
        arguments, error = validate_tool_arguments("delete_everything", {})
        self.assertIsNone(arguments)
        self.assertIn("no permitida", error)

    def test_reply_is_bounded_before_whatsapp_delivery(self):
        reply = bounded_customer_reply("Hola. " * 400)
        self.assertLessEqual(len(reply), 1500)
        self.assertTrue(reply.endswith(".") or reply.endswith("…"))

    @patch.object(agent, "_run_tool", return_value=[])
    @patch.object(agent, "_ask_deepseek")
    def test_fifth_tool_call_in_one_round_is_not_executed(self, ask_model, run_tool):
        calls = [
            {
                "id": "call-{}".format(index),
                "type": "function",
                "function": {
                    "name": "search_products",
                    "arguments": '{{"query":"Isabel {}"}}'.format(index),
                },
            }
            for index in range(5)
        ]
        ask_model.side_effect = [
            {"content": "", "tool_calls": calls, "_fred_usage": {}},
            {"content": "Te ayudo 😊", "_fred_usage": {}},
        ]
        result = agent.answer("Busco pestañas", history=[])
        self.assertEqual(run_tool.call_count, 4)
        self.assertEqual(result["reply"], "Te ayudo 😊")

    @patch.object(agent, "_run_tool", return_value=[])
    @patch.object(agent, "_ask_deepseek")
    def test_duplicate_tool_call_is_not_executed_twice(self, ask_model, run_tool):
        repeated = {
            "type": "function",
            "function": {"name": "search_products", "arguments": '{"query":"Isabel"}'},
        }
        ask_model.side_effect = [
            {
                "content": "",
                "tool_calls": [
                    {**repeated, "id": "call-one"},
                    {**repeated, "id": "call-two"},
                ],
                "_fred_usage": {},
            },
            {"content": "Te ayudo 😊", "_fred_usage": {}},
        ]
        agent.answer("Busco Isabel", history=[])
        run_tool.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
