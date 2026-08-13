"""Tests for the Isa-handoff audit fixes:

1. _queue_for_isa now attaches recent conversation_context for every
   escalation type (not just purchase_review/special_sale_request) -- the
   most common "clienta pidió hablar con Isa" case was previously excluded.
2. _pending_action_text actually renders that context in the WhatsApp card,
   where before it was captured into the payload but never shown to Isa.
3. wait_for_isa_response only ever lets one pending case await her next
   free-text reply at a time, so a second "Responder a Fred" tap can't
   cause her answer to be delivered to the wrong customer.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
import conversation_store  # noqa: E402


class QueueForIsaContextTests(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "Hola, tengo dudas sobre Isabel I"},
        {"role": "assistant", "content": "Sí, encontré SHOOW TOOLS - ISABEL I."},
        {"role": "user", "content": "Quiero hablar con Isa para que me recomiende"},
    ]

    @patch.object(app, "send_isa_pending_notification")
    @patch.object(app, "send_isa_pending_buttons", return_value=True)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "create_pending_action", return_value=42)
    @patch.object(app, "pending_action_count", return_value=0)
    def test_human_handoff_now_carries_conversation_context(
        self, count, create_action, set_state, send_buttons, send_notification,
    ):
        app._queue_for_isa(
            conversation_id=1, customer_phone="5491111111111",
            action_type="human_handoff", summary="Quiere hablar con Isa",
            customer_message="Quiero hablar con Isa para que me recomiende",
            conversation_context=self.HISTORY,
        )
        payload = create_action.call_args.kwargs["payload"]
        self.assertIn("conversation_context", payload)
        self.assertTrue(payload["conversation_context"])
        self.assertEqual(payload["conversation_context"][0]["speaker"], "Clienta")
        # A pending consultation is scoped, not a full takeover: Fred must
        # keep answering this same customer's other messages.
        set_state.assert_called_once_with(1, "BOT")

    @patch.object(app, "send_isa_pending_notification")
    @patch.object(app, "send_isa_pending_buttons", return_value=True)
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "create_pending_action", return_value=43)
    @patch.object(app, "pending_action_count", return_value=0)
    def test_bot_fallback_also_carries_conversation_context(
        self, count, create_action, set_state, send_buttons, send_notification,
    ):
        app._queue_for_isa(
            conversation_id=1, customer_phone="5491111111111",
            action_type="bot_fallback", summary="Fred no está seguro",
            customer_message="no sé qué es esto", conversation_context=self.HISTORY,
        )
        payload = create_action.call_args.kwargs["payload"]
        self.assertIn("conversation_context", payload)
        self.assertTrue(payload["conversation_context"])


class PendingActionTextRendersContextTests(unittest.TestCase):
    def test_renders_recent_context_when_present(self):
        action = {
            "id": 7, "action_type": "human_handoff", "summary": "Quiere hablar con Isa",
            "customer_phone": "5491111111111",
            "payload": {
                "customer_message": "Quiero hablar con Isa",
                "conversation_context": [
                    {"speaker": "Clienta", "body": "Hola, dudas sobre Isabel I"},
                    {"speaker": "Fred", "body": "Sí, encontré SHOOW TOOLS - ISABEL I."},
                ],
            },
        }
        text = app._pending_action_text(action)
        self.assertIn("Contexto reciente:", text)
        self.assertIn("Clienta: Hola, dudas sobre Isabel I", text)
        self.assertIn("Fred: Sí, encontré SHOOW TOOLS - ISABEL I.", text)

    def test_omits_context_section_when_absent(self):
        action = {
            "id": 8, "action_type": "human_handoff", "summary": "Quiere hablar con Isa",
            "customer_phone": "5491111111111",
            "payload": {"customer_message": "Quiero hablar con Isa"},
        }
        text = app._pending_action_text(action)
        self.assertNotIn("Contexto reciente:", text)


class _FakeCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self.rowcount = 1

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


class WaitForIsaResponseSingleFlightTests(unittest.TestCase):
    @patch.object(conversation_store, "_connect")
    def test_clears_other_awaiting_rows_before_setting_this_one(self, connect):
        cursor = _FakeCursor()
        connect.return_value = _FakeConnection(cursor)

        conversation_store.wait_for_isa_response(99)

        self.assertEqual(len(cursor.calls), 2)
        clear_sql, clear_params = cursor.calls[0]
        set_sql, set_params = cursor.calls[1]
        self.assertIn("payload - 'awaiting_isa_response'", clear_sql)
        self.assertIn("id != %s", clear_sql)
        self.assertEqual(clear_params, (99,))
        self.assertIn("jsonb_set(payload, '{awaiting_isa_response}'", set_sql)
        self.assertEqual(set_params, (99,))


class _IncomingRequest:
    def __init__(self, phone, text, message_id="wamid-test"):
        self._body = {
            "entry": [{"changes": [{"value": {"messages": [
                {"from": phone, "id": message_id, "text": {"body": text}},
            ]}}]}]
        }

    async def json(self):
        return self._body


@patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0)
class PendingConsultationNotFullTakeoverTests(unittest.TestCase):
    """Locks in the exact behaviour the audit confirmed but found untested:
    an explicit "ISA" conversation state (Isa took the case or paused Fred)
    silently ignores new customer messages. Combined with
    QueueForIsaContextTests.set_state.assert_called_once_with(1, "BOT")
    above (queuing a case does NOT flip to "ISA"), this is the full contract
    for "pending consultation, not full takeover": Fred only goes silent
    when Isa explicitly says so, never merely because a case is pending."""

    PHONE = "5491111111111"

    def _post(self, text, message_id="wamid-test"):
        return asyncio.run(app.webhook_post(_IncomingRequest(self.PHONE, text, message_id)))

    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "record_inbound_message", return_value=(7, "ISA", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_isa_state_silently_ignores_a_new_customer_message(self, history, inbound, send_message):
        with patch.object(app, "answer") as ask_model, patch.object(app, "search_similar_products") as retrieve:
            response = self._post("¿Cuánto sale?", "wamid-isa-state")

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        retrieve.assert_not_called()
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
