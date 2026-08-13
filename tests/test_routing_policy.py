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


class GroundedDiscoveryBypassTests(unittest.TestCase):
    """D1.1: a missing bare 'sku' argument must not erase a discovery answer
    that already resolved a real match against a verified live Tiendanube
    fact this same turn. Every other case keeps the pre-D1.1 behaviour."""

    GROUNDED_REPLY = (
        "¡Hola! Sí, encontré SHOOW TOOLS - NATURAL SHOOW (10 PAIRS). "
        "Está disponible. Sale $36.000. ¿Querés que te cuente algún detalle más? 😊"
    )
    SKU_MISSING = (DynamicRequirement(
        fact="live_price", verifier="get_stock",
        status="missing_arguments", missing_arguments=("sku",),
    ),)

    def test_grounded_discovery_with_missing_sku_preserves_the_reply(self):
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"], "checks_completed": ["live_price"],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, self.GROUNDED_REPLY)

    def test_discovery_without_verified_candidates_still_gets_the_generic_gate(self):
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"],
                "checks_completed": [],  # nothing was actually verified live
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el modelo exacto o su link.")

    def test_required_check_pending_blocks_bypass_even_with_other_checks_completed(self):
        # The exact production failure this audit traced: live_stock got
        # verified (e.g. via search_available_products), but the customer
        # asked for price and live_price was never actually checked. Having
        # *some* non-empty checks_completed must not be enough.
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"],
                "checks_completed": ["live_stock"],  # something else was verified, not this
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el modelo exacto o su link.")

    def test_stock_only_requirement_is_satisfied_by_a_completed_stock_check(self):
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_stock"],
                "checks_completed": ["live_stock"],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, self.GROUNDED_REPLY)

    def test_no_required_checks_never_blocks_on_an_unrelated_missing_sku_guess(self):
        # The decision itself needed no live check at all (e.g. the customer
        # never asked about price or stock in a way this decision tracked).
        # An empty required_checks set is vacuously satisfied.
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": [], "checks_completed": [],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, self.GROUNDED_REPLY)

    def test_lookup_with_missing_sku_keeps_current_behaviour(self):
        # A genuine lookup never sets response_mode=product_discovery in the
        # effective decision the way this bypass expects; it must not be
        # rescued just because a live check happened to complete.
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "checks_completed": ["live_price"],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            "Un modelo cualquiera.", routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el modelo exacto o su link.")

    def test_real_ambiguity_no_match_keeps_current_behaviour(self):
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "no_match",
                "checks_completed": [],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            "No encontré ese modelo.", routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el modelo exacto o su link.")

    def test_handoff_keeps_priority_over_the_bypass(self):
        routing = {
            "decision": {
                "action": "handoff_to_isa", "reason": "unable_to_verify",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"], "checks_completed": ["live_price"],
            },
            "handoff": {"reason": "unable_to_verify"},
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertNotEqual(reply, self.GROUNDED_REPLY)
        self.assertIn("Isa", reply)
        self.assertIn("modelo exacto o su link", reply)

    def test_unverified_price_is_never_let_through_as_fact(self):
        # Same shape as a grounded case, but nothing was actually verified
        # (checks_completed empty) and the model still claims a match. The
        # gate must win: no invented price or stock reaches the customer.
        claimed_price_reply = "Sale $99.999, tenemos stock. 😊"
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"], "checks_completed": [],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            claimed_price_reply, routing, dynamic_requirements=self.SKU_MISSING,
        )
        self.assertNotIn("99.999", reply)
        self.assertEqual(reply, "Para poder verificarlo en vivo, me falta el modelo exacto o su link.")

    def test_bypass_never_swallows_an_unrelated_missing_argument(self):
        # If a second, unrelated argument is also missing (e.g. order_number
        # from a different topic in the same turn), the narrow "sku"
        # exception must not accidentally absorb it.
        routing = {
            "decision": {
                "action": "reply", "reason": "normal_response",
                "response_mode": "product_discovery", "match_type": "exact_match",
                "required_checks": ["live_price"], "checks_completed": ["live_price"],
            },
            "handoff": None,
        }
        reply = align_reply_with_routing(
            self.GROUNDED_REPLY, routing,
            dynamic_requirements=(DynamicRequirement(
                fact="live_price", verifier="get_stock",
                status="missing_arguments", missing_arguments=("sku", "order_number"),
            ),),
        )
        self.assertNotEqual(reply, self.GROUNDED_REPLY)
        self.assertIn("número de orden", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
