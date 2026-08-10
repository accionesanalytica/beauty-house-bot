"""Checks that Fred's curated evaluation set stays useful and safe."""

import sys
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from fred_eval_cases import CURATED_CASES  # noqa: E402


class CuratedEvalSuiteTests(unittest.TestCase):
    def test_has_at_least_fifty_distinct_realistic_cases(self):
        self.assertGreaterEqual(len(CURATED_CASES), 50)
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
