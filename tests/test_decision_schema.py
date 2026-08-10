"""Offline tests for the structured decision boundary."""

import sys
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from decision_schema import build_effective_decision, validate_model_decision  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
