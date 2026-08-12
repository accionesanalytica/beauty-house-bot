"""Local tests for deterministic Knowledge V1 live checks."""

import sys
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from dynamic_checks import execute_dynamic_requirements  # noqa: E402
from knowledge_rag import DynamicRequirement  # noqa: E402


class DynamicChecksTests(unittest.TestCase):
    def test_ready_order_status_executes_with_exact_argument(self):
        calls = []

        def get_order_status(order_number):
            calls.append(order_number)
            return {"found": True, "order_number": order_number, "status": "open"}

        outcomes = execute_dynamic_requirements((DynamicRequirement(
            fact="order_status", verifier="get_order_status", status="ready",
            required_arguments=("order_number",),
            arguments={"order_number": "12345"},
        ),), {"get_order_status": get_order_status})

        self.assertEqual(calls, ["12345"])
        self.assertEqual(outcomes[0].status, "completed")
        self.assertEqual(outcomes[0].result["order_number"], "12345")

    def test_ready_stock_executes_with_exact_sku(self):
        outcomes = execute_dynamic_requirements((DynamicRequirement(
            fact="live_price", verifier="get_stock", status="ready",
            required_arguments=("sku",), arguments={"sku": "SKU-1"},
        ),), {"get_stock": lambda sku: {"sku": sku, "price": "30000"}})

        self.assertEqual(outcomes[0].status, "completed")
        self.assertEqual(outcomes[0].result["price"], "30000")

    def test_missing_argument_never_calls_tool(self):
        called = []
        outcomes = execute_dynamic_requirements((DynamicRequirement(
            fact="order_status", verifier="get_order_status",
            status="missing_arguments", required_arguments=("order_number",),
            missing_arguments=("order_number",),
        ),), {"get_order_status": lambda **kwargs: called.append(kwargs)})

        self.assertFalse(called)
        self.assertEqual(outcomes[0].status, "missing_arguments")
        self.assertEqual(outcomes[0].missing_arguments, ("order_number",))

    def test_unknown_or_unavailable_tool_never_executes(self):
        called = []
        outcomes = execute_dynamic_requirements((DynamicRequirement(
            fact="calendar_availability", verifier="calendar_tool", status="ready",
        ),), {"calendar_tool": lambda: called.append(True)})

        self.assertFalse(called)
        self.assertEqual(outcomes[0].status, "unavailable_tool")

    def test_tool_failure_is_explicit_and_does_not_return_fake_result(self):
        def failing_get_stock(sku):
            raise RuntimeError("offline")

        outcomes = execute_dynamic_requirements((DynamicRequirement(
            fact="live_stock", verifier="get_stock", status="ready",
            arguments={"sku": "SKU-1"},
            customer_fallback="No pude confirmar el stock en vivo.",
        ),), {"get_stock": failing_get_stock})

        self.assertEqual(outcomes[0].status, "failed")
        self.assertFalse(outcomes[0].result)
        self.assertEqual(outcomes[0].error, "RuntimeError")


if __name__ == "__main__":
    unittest.main(verbosity=2)
