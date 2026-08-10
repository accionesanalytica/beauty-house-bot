"""Baseline checks for Fred's real webhook orchestration.

These tests exercise ``app.webhook_post`` from the inbound WhatsApp payload to
the outbound response, while replacing Meta, DeepSeek, Tiendanube and
Supabase with local doubles.  They are deliberately cost-free and never make
network calls.

They complement unit tests: a passing agent-only evaluation is not enough if
the surrounding state, escalation or fallback orchestration is incorrect.
"""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
import app  # noqa: E402


class IncomingRequest:
    """Minimal async request double accepted by FastAPI's webhook function."""

    def __init__(self, phone, text, message_id="wamid-test"):
        self._body = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": phone,
                                        "id": message_id,
                                        "text": {"body": text},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

    async def json(self):
        return self._body


class WebhookHarnessTests(unittest.TestCase):
    PHONE = "5491111111111"

    def _post(self, text, message_id="wamid-test"):
        return asyncio.run(app.webhook_post(IncomingRequest(self.PHONE, text, message_id)))

    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_social_greeting_uses_no_model_or_retrieval(
        self, history, inbound, send_message, record_message
    ):
        with patch.object(app, "answer") as ask_model, patch.object(app, "search_similar_products") as retrieve:
            response = self._post("hola")

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        retrieve.assert_not_called()
        send_message.assert_called_once_with(self.PHONE, "¡Hola! 😊 ¿En qué te puedo ayudar?")
        record_message.assert_called_once_with(7, "¡Hola! 😊 ¿En qué te puedo ayudar?")

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[{"role": "user", "content": "Busco algo natural"}])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_standard_turn_builds_context_then_delivers_agent_reply(
        self, history, inbound, send_message, record_message, record_turn
    ):
        agent_result = {
            "reply": "Encontré una opción que puede servirte 😊",
            "tool_calls": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4},
        }
        with patch.object(app, "search_similar_products", return_value="Productos encontrados: Isabel I") as retrieve, patch.object(
            app, "answer", return_value=agent_result
        ) as ask_model:
            response = self._post("Busco pestañas naturales", "wamid-standard")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_called_once_with("Busco pestañas naturales")
        self.assertEqual(ask_model.call_args.kwargs["history"], [{"role": "user", "content": "Busco algo natural"}])
        self.assertEqual(ask_model.call_args.kwargs["rag_context"], "Productos encontrados: Isabel I")
        self.assertTrue(ask_model.call_args.kwargs["greeting_required"])
        send_message.assert_called_once_with(self.PHONE, agent_result["reply"])
        record_message.assert_called_once_with(7, agent_result["reply"])
        record_turn.assert_called_once()
        observation = record_turn.call_args.kwargs
        self.assertEqual(observation["source_message_id"], "wamid-standard")
        self.assertEqual(observation["conversation_id"], 7)
        self.assertEqual(observation["action"], "reply")
        self.assertEqual(observation["outcome"], "replied")
        self.assertTrue(observation["catalog_context_used"])
        self.assertFalse(observation["knowledge_context_used"])
        self.assertGreaterEqual(observation["duration_ms"], 0)

    @patch.object(app, "record_agent_turn", side_effect=RuntimeError("database unavailable"))
    def test_observability_failure_is_non_blocking(self, record_turn):
        app._record_agent_turn_safely(
            wa_message_id="wamid-observation-failure",
            conversation_id=7,
            result={"tool_calls": [], "usage": {}},
            action="reply",
            reason="normal_response",
            outcome="replied",
            catalog_context_used=False,
            knowledge_context_used=False,
            duration_ms=10,
        )

        record_turn.assert_called_once()

    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_direct_human_request_escalates_without_model_or_retrieval(
        self, history, inbound, queue_for_isa, send_message, record_message
    ):
        with patch.object(app, "answer") as ask_model, patch.object(app, "search_similar_products") as retrieve:
            response = self._post("Pasame con Isa", "wamid-handoff")

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        retrieve.assert_not_called()
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "human_handoff")
        self.assertIn("se lo paso a Isa", send_message.call_args.args[1])
        record_message.assert_called_once()

    @patch.object(app, "_send_service_fallback")
    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_agent_error_uses_safe_service_fallback(
        self, history, inbound, record_turn, fallback
    ):
        with patch.object(app, "search_similar_products", return_value="") as retrieve, patch.object(
            app, "answer", side_effect=RuntimeError("provider unavailable")
        ):
            response = self._post("¿Tenés Isabel I?", "wamid-provider-error")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_called_once()
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[0:3], (self.PHONE, 7, "¿Tenés Isabel I?"))
        self.assertEqual(record_turn.call_args.kwargs["action"], "service_fallback")

    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", True))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_duplicate_meta_message_never_generates_second_reply(
        self, history, inbound, send_message
    ):
        with patch.object(app, "answer") as ask_model, patch.object(app, "search_similar_products") as retrieve:
            response = self._post("Necesito ayuda", "wamid-duplicate")

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        retrieve.assert_not_called()
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
