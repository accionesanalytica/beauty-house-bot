"""Tests for the pure routing policy shared by production and shadow."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from knowledge_rag import DynamicRequirement, KnowledgeObligations, KnowledgeRetrieval  # noqa: E402
from routing_policy import align_reply_with_routing, resolve_harness_routing  # noqa: E402


class RoutingPolicyTests(unittest.TestCase):
    def test_primary_topic_obligation_forces_handoff(self):
        retrieval = KnowledgeRetrieval(
            governing_topic="order_tracking",
            obligations=KnowledgeObligations(
                topics=("order_tracking",), escalation_required=True
            ),
        )
        result = resolve_harness_routing(
            "El correo no mueve mi paquete hace una semana", [],
            decision={"action": "reply", "reason": "normal_response"},
            knowledge_retrieval=retrieval,
        )
        self.assertEqual(result["decision"]["action"], "handoff_to_isa")
        self.assertEqual(result["source"], "primary_topic_obligation")

    def test_secondary_topic_cannot_force_handoff(self):
        retrieval = KnowledgeRetrieval(
            governing_topic="commercial_operations",
            retrieved_topics=("commercial_operations", "order_tracking"),
            obligations=KnowledgeObligations(topics=("commercial_operations",)),
        )
        result = resolve_harness_routing(
            "¿Cómo puedo pagar un encargo?", [],
            decision={"action": "reply", "reason": "normal_response"},
            knowledge_retrieval=retrieval,
        )
        self.assertEqual(result["decision"]["action"], "reply")
        self.assertIsNone(result["handoff"])

    def test_legacy_boundary_remains_when_knowledge_has_no_primary(self):
        result = resolve_harness_routing(
            "Quiero encargar un producto", [],
            decision={"action": "reply", "reason": "normal_response"},
            knowledge_retrieval=KnowledgeRetrieval(),
        )
        self.assertEqual(result["decision"]["action"], "handoff_to_isa")
        self.assertEqual(result["source"], "legacy_safety_fallback")

    def test_handoff_is_visible_with_reason_and_next_step(self):
        routing = {
            "decision": {"action": "handoff_to_isa", "reason": "unable_to_verify"},
            "handoff": {"reason": "unable_to_verify"},
        }
        reply = align_reply_with_routing(
            "Necesito el número de orden.", routing,
            dynamic_requirements=(DynamicRequirement(
                fact="order_status", verifier="get_order_status",
                status="missing_arguments", missing_arguments=("order_number",),
            ),),
        )
        self.assertIn("Isa", reply)
        self.assertIn("número de orden", reply)
        self.assertIn("se lo paso", reply)
        self.assertNotIn("Necesito el número", reply)

    def test_missing_live_argument_asks_only_for_that_argument(self):
        reply = align_reply_with_routing(
            "Contame también tu email y dirección.",
            {"decision": {"action": "reply", "reason": "normal_response"}, "handoff": None},
            dynamic_requirements=(DynamicRequirement(
                fact="order_status", verifier="get_order_status",
                status="missing_arguments", missing_arguments=("order_number",),
            ),),
        )
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el número de orden.")
        self.assertNotIn("email", reply)

    def test_unavailable_tool_explains_limit_without_inventing(self):
        reply = align_reply_with_routing(
            "Podés pasar hoy a las 15.",
            {"decision": {"action": "reply", "reason": "normal_response"}, "handoff": None},
            dynamic_requirements=(DynamicRequirement(
                fact="calendar_availability", verifier="unavailable_tool",
                status="unavailable_tool",
                customer_fallback="No puedo confirmar horarios disponibles en vivo desde acá.",
            ),),
        )
        self.assertIn("No puedo confirmar horarios", reply)

    def test_unavailable_requirement_replaces_unrelated_model_clarification(self):
        reply = align_reply_with_routing(
            "Información mayorista aprobada.\nhttps://example.com/mayorista",
            {"decision": {"action": "clarify_product", "reason": "missing_product"}, "handoff": None},
            dynamic_requirements=(DynamicRequirement(
                fact="current_courier_quote", verifier="unavailable_tool",
                status="unavailable_tool",
                customer_fallback="No puedo confirmar la cotización actual del courier automáticamente.",
            ),),
        )
        self.assertIn("https://example.com/mayorista", reply)
        self.assertIn("No puedo confirmar la cotización", reply)
        self.assertIn("modelo exacto", reply)

    def test_unavailable_live_fact_requiring_isa_changes_effective_route(self):
        requirement = SimpleNamespace(
            status="unavailable_tool",
            customer_fallback="No puedo confirmar la cotización actual; la revisa Isa.",
        )
        routing = resolve_harness_routing(
            "¿Cuánto cuesta el courier?", [],
            decision={"action": "clarify_product", "reason": "product_ambiguity"},
            dynamic_requirements=[requirement],
        )
        self.assertEqual(routing["decision"]["action"], "handoff_to_isa")
        self.assertEqual(routing["source"], "dynamic_requirement_fallback")
        reply = align_reply_with_routing(
            "¿Qué producto buscás?", routing,
            dynamic_requirements=[requirement],
        )
        self.assertNotIn("Qué producto", reply)
        self.assertIn("cotización actual", reply)
        self.assertIn("Se lo paso", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
