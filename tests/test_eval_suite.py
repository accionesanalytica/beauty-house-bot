"""Checks that Fred's curated evaluation set stays useful and safe."""

import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parents[0] / "bot"))

from fred_eval_cases import CURATED_CASES  # noqa: E402
from evaluation import assess_case  # noqa: E402


class CuratedEvalSuiteTests(unittest.TestCase):
    def test_has_at_least_fifty_distinct_realistic_cases(self):
        self.assertGreaterEqual(len(CURATED_CASES), 60)
        self.assertEqual(len({case.case_id for case in CURATED_CASES}), len(CURATED_CASES))

    def test_every_case_has_a_human_review_note(self):
        for case in CURATED_CASES:
            self.assertTrue(case.notes, case.case_id)
            self.assertTrue(case.customer_message, case.case_id)

    def test_payment_scenarios_require_escalation(self):
        # Reclamos y preventas pueden pedir primero un dato inocuo (por ejemplo,
        # número de orden). Pagos sí deben derivarse antes de afirmar condiciones.
        sensitive_categories = {"pagos"}
        for case in CURATED_CASES:
            if case.category in sensitive_categories:
                self.assertTrue(case.should_escalate, case.case_id)

    def test_sensitive_case_requires_structured_handoff_not_just_the_word_isa(self):
        case = next(case for case in CURATED_CASES if case.case_id == "handoff-human-01")
        assessment = assess_case(case, {"reply": "Te paso con Isa.", "decision": {"action": "reply"}})
        self.assertEqual(assessment["score"], 55)
        self.assertTrue(assessment["findings"])

    def test_verified_structured_handoff_scores_cleanly(self):
        case = next(case for case in CURATED_CASES if case.case_id == "handoff-human-01")
        assessment = assess_case(case, {
            "reply": "Dale, se lo paso a Isa 😊",
            "decision": {"action": "handoff_to_isa"},
            "tool_calls": [{"name": "request_isa_handoff"}],
        })
        self.assertEqual(assessment["score"], 100)

    def test_lifting_without_product_must_request_a_model_or_link(self):
        case = next(case for case in CURATED_CASES if case.case_id == "advisory-lifting-01")
        assessment = assess_case(case, {
            "reply": "Para confirmarlo bien, pasame el nombre del modelo o el link del producto 😊",
            "decision": {"action": "reply"},
        })
        self.assertEqual(assessment["score"], 100)

        missing = assess_case(case, {
            "reply": "No te lo puedo asegurar todavía.",
            "decision": {"action": "reply"},
        })
        self.assertEqual(missing["score"], 80)
