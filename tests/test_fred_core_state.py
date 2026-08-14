"""Tests for Fred Core's persistence layer: the single source of truth for
conversation state (mode + structured fields), never reconstructed by
reading Fred's own prior message text.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import conversation_store  # noqa: E402


class _FakeCursor:
    def __init__(self, fetch_result=None):
        self.calls = []
        self._fetch_result = fetch_result

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetch_result

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class GetFredCoreStateTests(unittest.TestCase):
    @patch.object(conversation_store, "_connect")
    def test_missing_row_defaults_to_chat_with_nothing_else_set(self, connect):
        connect.return_value = _FakeConnection(_FakeCursor(fetch_result=None))
        state = conversation_store.get_fred_core_state(7)
        self.assertEqual(state["mode"], "CHAT")
        self.assertIsNone(state["active_product_id"])
        self.assertIsNone(state["quantity"])

    @patch.object(conversation_store, "_connect")
    def test_existing_row_is_returned_field_for_field(self, connect):
        row = ("CHECKOUT", "123", "SHOOW TOOLS - ISABEL I", "ISABEL-1", "8mm",
               "30000.00", 4, "shipping", "Ana Pérez", "ana@example.com",
               "1414", "delivery_method", None)
        connect.return_value = _FakeConnection(_FakeCursor(fetch_result=row))
        state = conversation_store.get_fred_core_state(7)
        self.assertEqual(state["mode"], "CHECKOUT")
        self.assertEqual(state["active_product_name"], "SHOOW TOOLS - ISABEL I")
        self.assertEqual(state["quantity"], 4)
        self.assertEqual(state["checkout_step"], "delivery_method")


class SaveFredCoreStateTests(unittest.TestCase):
    @patch.object(conversation_store, "_connect")
    def test_upsert_only_writes_the_given_fields(self, connect):
        cursor = _FakeCursor()
        connect.return_value = _FakeConnection(cursor)

        conversation_store.save_fred_core_state(7, mode="CHECKOUT", quantity=4)

        self.assertEqual(len(cursor.calls), 1)
        sql, params = cursor.calls[0]
        self.assertIn("INSERT INTO fred_core_state (conversation_id, mode, quantity)", sql)
        self.assertIn("mode = EXCLUDED.mode, quantity = EXCLUDED.quantity", sql)
        self.assertEqual(params, [7, "CHECKOUT", 4])

    @patch.object(conversation_store, "_connect")
    def test_unknown_field_is_rejected_before_touching_the_database(self, connect):
        with self.assertRaises(ValueError):
            conversation_store.save_fred_core_state(7, not_a_real_field="x")
        connect.assert_not_called()

    @patch.object(conversation_store, "_connect")
    def test_no_fields_is_a_no_op(self, connect):
        conversation_store.save_fred_core_state(7)
        connect.assert_not_called()


class ResetFredCoreCheckoutTests(unittest.TestCase):
    @patch.object(conversation_store, "save_fred_core_state")
    def test_clears_checkout_fields_but_keeps_active_product_and_order_number(self, save_state):
        conversation_store.reset_fred_core_checkout(7)
        save_state.assert_called_once_with(
            7, mode="CHAT", quantity=None, delivery_method=None,
            customer_name=None, customer_email=None, postal_code=None,
            checkout_step=None,
        )
        called_fields = save_state.call_args.kwargs
        self.assertNotIn("active_product_id", called_fields)
        self.assertNotIn("order_number", called_fields)


if __name__ == "__main__":
    unittest.main()
