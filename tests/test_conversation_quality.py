"""Tests for the presentation-only WhatsApp conversation contract."""

import sys
import re
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from conversation_quality import apply_conversation_contract  # noqa: E402
from knowledge_rag import KnowledgeObligations, enforce_knowledge_obligations  # noqa: E402
from routing_policy import align_reply_with_routing  # noqa: E402


class ConversationQualityTests(unittest.TestCase):
    def test_preserves_required_disclosure_and_exact_link(self):
        reply = (
            "Los recipientes vienen vacíos y el kit no incluye cosméticos.\n"
            "https://www.instagram.com/reel/DOdyhBHje7w/\n\n"
            "¿Querés que te cuente algo más del kit?"
        )
        polished = apply_conversation_contract(
            reply,
            routing_contract={"next_step": "continue", "response_mode": "policy_answer"},
        )
        self.assertIn("vienen vacíos", polished)
        self.assertIn("no incluye cosméticos", polished)
        self.assertIn("https://www.instagram.com/reel/DOdyhBHje7w/", polished)
        self.assertNotIn("¿Querés que te cuente", polished)

    def test_does_not_repeat_greeting_after_first_assistant_turn(self):
        polished = apply_conversation_contract(
            "¡Hola! 😊 Te cuento: tenemos dos opciones disponibles.",
            history=[{"role": "assistant", "content": "¡Hola! ¿En qué te ayudo?"}],
            routing_contract={"next_step": "continue", "response_mode": "product_advice"},
        )
        self.assertEqual(polished, "Te cuento: tenemos dos opciones disponibles.")

    def test_keeps_clarification_question_when_information_is_missing(self):
        reply = "¿Me pasás el número de orden? Lo necesito para revisarlo."
        polished = apply_conversation_contract(
            reply,
            routing_contract={"next_step": "provide_missing_information"},
        )
        self.assertEqual(polished, reply)

    def test_removes_unneeded_question_even_when_required_link_follows(self):
        reply = (
            "El video muestra cómo cargarlo.\n\n"
            "¿Te quedó alguna duda?\n\n"
            "https://www.instagram.com/reel/DOdyhBHje7w/"
        )
        polished = apply_conversation_contract(
            reply,
            routing_contract={"next_step": "continue", "response_mode": "policy_answer"},
        )
        self.assertNotIn("¿Te quedó", polished)
        self.assertTrue(polished.endswith("https://www.instagram.com/reel/DOdyhBHje7w/"))

    def test_handoff_removes_question_only_when_no_information_is_missing(self):
        reply = "¿Qué adhesivo usaste?\n\nNo pude confirmarlo; lo revisa Isa."
        polished = apply_conversation_contract(
            reply,
            routing_contract={
                "next_step": "isa_review",
                "missing_information": [],
            },
        )
        self.assertNotIn("¿Qué adhesivo", polished)
        self.assertIn("Isa", polished)

    def test_keeps_product_discovery_question_when_no_match_exists(self):
        reply = "No encontré ese producto publicado. ¿Tenés el nombre exacto?"
        polished = apply_conversation_contract(
            reply,
            routing_contract={
                "next_step": "continue",
                "response_mode": "product_discovery",
                "match_type": "no_match",
            },
        )
        self.assertEqual(polished, reply)

    def test_removes_generic_question_when_resolved_legacy_reply_has_no_mode(self):
        polished = apply_conversation_contract(
            "El adhesivo es removible y no dura siete días. ¿Te sirve?",
            routing_contract={"next_step": "continue"},
        )
        self.assertEqual(polished, "El adhesivo es removible y no dura siete días.")

    def test_repeated_handoff_advances_instead_of_looping(self):
        repeated = "¿Me pasás el número de orden? Con eso lo reviso y se lo dejo a Isa con todo el contexto."
        polished = apply_conversation_contract(
            repeated,
            history=[{"role": "assistant", "content": repeated}],
            routing_contract={"next_step": "isa_review", "missing_information": []},
        )
        self.assertNotEqual(polished, repeated)
        self.assertIn("Isa", polished)
        self.assertIn("todo el contexto", polished)

    def test_handoff_mentions_isa_once_when_limit_already_names_her(self):
        polished = apply_conversation_contract(
            "No puedo confirmarlo; lo puede revisar Isa.\n\n"
            "Se lo paso a Isa para que lo revise y seguimos por acá.",
            routing_contract={"next_step": "isa_review", "missing_information": []},
        )
        self.assertEqual(len(re.findall(r"\bIsa\b", polished, flags=re.IGNORECASE)), 1)
        self.assertIn("todo el contexto", polished)

    def test_naturalises_handoff_without_changing_isa_decision(self):
        polished = apply_conversation_contract(
            "No pude confirmar el dato.\n\nSe lo paso con lo que ya me contaste y seguimos por acá.",
            routing_contract={
                "action": "handoff_to_isa",
                "next_step": "isa_review",
            },
        )
        self.assertIn("Isa", polished)
        self.assertIn("seguimos por acá", polished)
        self.assertNotIn("con lo que ya me contaste", polished)

    def test_never_loses_or_reconstructs_a_url(self):
        reply = (
            "Mirá el producto acá: https://beautyhousemakeup.com/productos/demo/?x=1.\n\n"
            "¿Querés que te muestre otra opción?"
        )
        polished = apply_conversation_contract(
            reply,
            routing_contract={"next_step": "continue", "response_mode": "product_advice"},
        )
        self.assertIn("https://beautyhousemakeup.com/productos/demo/?x=1.", polished)

    def test_routing_replacement_cannot_erase_approved_obligations(self):
        routed = align_reply_with_routing(
            "El showroom está cerrado.",
            {
                "decision": {"action": "handoff_to_isa", "reason": "unable_to_verify"},
                "handoff": {"reason": "unable_to_verify"},
            },
            dynamic_requirements=[],
        )
        final = enforce_knowledge_obligations(
            routed,
            KnowledgeObligations(
                required_disclosures=({
                    "id": "showroom-closed",
                    "text": "El showroom está cerrado y los retiros requieren reserva previa.",
                    "required_terms": ["showroom", "cerrado", "reserva previa"],
                },),
                required_links=({
                    "id": "pickup-calendar",
                    "url": "https://calendar.app.google/demo",
                },),
            ),
        )
        self.assertIn("showroom está cerrado", final)
        self.assertIn("reserva previa", final)
        self.assertIn("https://calendar.app.google/demo", final)


if __name__ == "__main__":
    unittest.main(verbosity=2)
