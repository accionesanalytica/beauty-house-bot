"""A handoff's ``summary`` is audit material, never conversation.

Every escalation carries two strings with different audiences: the summary is
written for Isa and for the audit trail (it names topics, routing sources and
verifiers), while the customer reads a fixed sentence chosen from the
deterministic ``reason``.  Production replied "El topic aprobado requiere
revisión de Isa para este caso" to a real customer because those two were the
same string.

These tests are the guard against that class of bug -- not against that one
sentence.  The routing summaries are harvested from ``routing_policy.py``
itself, so a NEW internal summary added later is covered the day it is written
without anyone remembering to extend this file.
"""

import asyncio
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


ROUTING_POLICY_SOURCE = (BOT_DIR / "routing_policy.py").read_text(encoding="utf-8")

# Every "summary": "<literal>" written in the routing policy. Harvested from
# source on purpose: this is the set of strings that can reach the handoff
# dict, and it must stay in sync automatically.
ROUTING_SUMMARIES = tuple(
    sorted(set(re.findall(r'"summary":\s*"([^"]+)"', ROUTING_POLICY_SOURCE)))
)

# Vocabulary that only ever appears in internal state, never in something a
# person would say to a customer over WhatsApp.
INTERNAL_VOCABULARY = ("topic", "verificador", "routing", "summary")


def _default_fred_core_state(conversation_id):
    return {
        "mode": "CHAT", "active_product_id": None, "active_product_name": None,
        "active_sku": None, "active_variant": None, "unit_price": None,
        "quantity": None, "delivery_method": None, "customer_name": None,
        "customer_email": None, "postal_code": None, "checkout_step": None,
        "order_number": None,
    }


class IncomingRequest:
    def __init__(self, phone, text, message_id="wamid-internal"):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": phone, "id": message_id, "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


class HandoffLeadUnitTests(unittest.TestCase):
    """The customer-facing sentence comes from the reason, and only from it."""

    def test_every_known_reason_has_its_own_natural_sentence(self):
        for reason in ("special_sale_request", "human_request", "purchase_intent", "unable_to_verify"):
            lead = app._isa_handoff_lead(reason)
            self.assertTrue(lead.strip())
            self.assertNotIn("_", lead, "reason keys must never surface as copy")
            for word in INTERNAL_VOCABULARY:
                self.assertNotIn(word, lead.lower())

    def test_unknown_or_missing_reason_falls_back_to_a_neutral_sentence(self):
        for reason in (None, "", "something_new_nobody_mapped_yet"):
            self.assertEqual(app._isa_handoff_lead(reason), app._ISA_HANDOFF_DEFAULT_LEAD)

    def test_no_routing_summary_is_reachable_as_a_customer_sentence(self):
        leads = set(app._ISA_HANDOFF_LEADS.values()) | {app._ISA_HANDOFF_DEFAULT_LEAD}
        for summary in ROUTING_SUMMARIES:
            self.assertNotIn(summary, leads)

    def test_the_harvest_actually_found_the_routing_summaries(self):
        # Protects the tests below from silently degrading into no-ops if the
        # literals in routing_policy.py are ever reformatted.
        self.assertGreaterEqual(len(ROUTING_SUMMARIES), 6)
        self.assertIn(
            "El topic aprobado requiere revisión de Isa para este caso.",
            ROUTING_SUMMARIES,
        )


@patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0)
@patch.object(app, "get_fred_core_state", _default_fred_core_state)
@patch.object(app, "save_fred_core_state", lambda *args, **kwargs: None)
@patch.object(app, "reset_fred_core_checkout", lambda conversation_id: None)
@patch.object(app, "get_active_sales_intake", lambda conversation_id: None)
class InternalTextNeverDeliveredTests(unittest.TestCase):
    PHONE = "5491111111111"

    def _deliver_with_handoff(self, handoff, send_message):
        """Run one real agent turn whose routing resolves to ``handoff`` and
        return exactly what would have gone out over WhatsApp."""
        routing = {
            "decision": {
                "action": "handoff_to_isa",
                "reason": handoff["reason"],
                "summary": handoff["summary"],
            },
            "handoff": handoff,
            "source": "primary_topic_obligation",
            "governing_topic": "commercial_operations",
        }
        agent_result = {
            "reply": "Te cuento lo que sé de las devoluciones 😊",
            "tool_calls": [], "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        with patch.object(app, "search_similar_products", return_value=""), \
                patch.object(app, "answer", return_value=agent_result), \
                patch.object(app, "resolve_harness_routing", return_value=routing):
            response = asyncio.run(app.webhook_post(
                IncomingRequest(self.PHONE, "quiero devolver un producto", "wamid-internal")
            ))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(send_message.called, "the turn produced no outgoing message")
        return send_message.call_args.args[1]

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_no_routing_summary_is_ever_sent_to_the_customer(
        self, history, inbound, send_message, record_message, record_turn
    ):
        for summary in ROUTING_SUMMARIES:
            for reason in ("unable_to_verify", "special_sale_request", "human_request"):
                with self.subTest(summary=summary, reason=reason):
                    send_message.reset_mock()
                    delivered = self._deliver_with_handoff(
                        {"reason": reason, "summary": summary}, send_message,
                    )
                    self.assertNotIn(summary, delivered)

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_no_internal_vocabulary_reaches_the_customer(
        self, history, inbound, send_message, record_message, record_turn
    ):
        for summary in ROUTING_SUMMARIES:
            with self.subTest(summary=summary):
                send_message.reset_mock()
                delivered = self._deliver_with_handoff(
                    {"reason": "unable_to_verify", "summary": summary}, send_message,
                ).lower()
                for word in INTERNAL_VOCABULARY:
                    self.assertNotIn(word, delivered)

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_a_model_written_summary_for_isa_is_not_customer_copy_either(
        self, history, inbound, send_message, record_message, record_turn
    ):
        # request_isa_handoff lets the MODEL write the summary. It is addressed
        # to Isa ("la clienta dice que...", third person) and reads as
        # backstage notes, so it must not be forwarded verbatim any more than a
        # routing string is.
        model_summary = (
            "La clienta reclama un producto fallado; requiere verificacion "
            "manual de Isa y revision del pedido en Tiendanube."
        )
        delivered = self._deliver_with_handoff(
            {"reason": "unable_to_verify", "summary": model_summary}, send_message,
        )
        self.assertNotIn(model_summary, delivered)
        self.assertNotIn("verificacion manual", delivered.lower())

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_the_customer_still_gets_a_real_sentence_and_isas_number(
        self, history, inbound, send_message, record_message, record_turn
    ):
        # Removing the leak must not degrade into silence: what replaces the
        # summary still has to be a usable answer with a way forward.
        delivered = self._deliver_with_handoff(
            {
                "reason": "unable_to_verify",
                "summary": "El topic aprobado requiere revisión de Isa para este caso.",
            },
            send_message,
        )
        self.assertIn("Isa", delivered)
        self.assertIn(app.isa_contact_number(), delivered)
        self.assertGreater(len(delivered.strip()), 40)


if __name__ == "__main__":
    unittest.main()
