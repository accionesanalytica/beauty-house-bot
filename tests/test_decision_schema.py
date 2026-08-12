"""Offline tests for the structured decision boundary."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from decision_schema import build_effective_decision, validate_model_decision  # noqa: E402
from tool_guardrails import bounded_customer_reply, validate_tool_arguments  # noqa: E402
import agent  # noqa: E402


class DecisionSchemaTests(unittest.TestCase):
    @staticmethod
    def _decision_call(payload, call_id="decision-1"):
        import json
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "set_turn_decision",
                "arguments": json.dumps(payload, ensure_ascii=False),
            },
        }

    @staticmethod
    def _product_decision(
        match_type,
        requested,
        matched="",
        required_checks=None,
        requested_type=None,
        matched_type=None,
    ):
        return {
            "action": "reply",
            "reason": "normal_response",
            "summary": "Clasificó la relación comercial antes de responder.",
            "response_mode": "product_discovery",
            "match_type": match_type,
            "requested_product": requested,
            "matched_product": matched,
            "requested_product_type": requested_type or requested,
            "matched_product_type": "" if match_type == "no_match" else (matched_type or matched),
            "required_checks": required_checks or [],
        }

    def test_valid_normal_reply_is_accepted(self):
        decision = validate_model_decision({
            "action": "reply", "reason": "normal_response", "summary": "Orientó a la clienta.",
        })
        self.assertEqual(decision["action"], "reply")

    def test_malformed_or_unknown_model_decision_is_rejected(self):
        self.assertIsNone(validate_model_decision({"action": "create_checkout"}))
        self.assertIsNone(validate_model_decision({
            "action": "handoff_to_isa", "reason": "anything", "summary": "x",
        }))

    def test_sale_intake_requires_verified_candidate_not_model_claim(self):
        result = build_effective_decision({
            "action": "start_sales_intake", "reason": "purchase_intent", "summary": "La clienta compra.",
        })
        self.assertEqual(result["action"], "reply")

        result = build_effective_decision(
            None,
            sale_candidate={"sku": "ISABEL-1", "product_name": "Isabel I"},
        )
        self.assertEqual(result["action"], "start_sales_intake")

    def test_handoff_requires_existing_handoff_fact(self):
        result = build_effective_decision({
            "action": "handoff_to_isa", "reason": "human_request", "summary": "La pasa a Isa.",
        })
        self.assertEqual(result["action"], "reply")

        result = build_effective_decision(
            None, handoff={"reason": "human_request", "summary": "Pidió hablar con Isa."}
        )
        self.assertEqual(result["action"], "handoff_to_isa")

    def test_clarification_is_deterministic_after_tool_limit(self):
        result = build_effective_decision(None, needs_product_clarification=True)
        self.assertEqual(result["action"], "clarify_product")

    def test_tool_arguments_are_bounded_and_unknown_tools_are_rejected(self):
        arguments, error = validate_tool_arguments(
            "search_products", {"query": "Isabel I", "limit": 99}
        )
        self.assertIsNone(arguments)
        self.assertIn("Búsqueda inválida", error)
        arguments, error = validate_tool_arguments("delete_everything", {})
        self.assertIsNone(arguments)
        self.assertIn("no permitida", error)

    def test_reply_is_bounded_before_whatsapp_delivery(self):
        reply = bounded_customer_reply("Hola. " * 400)
        self.assertLessEqual(len(reply), 1500)
        self.assertTrue(reply.endswith(".") or reply.endswith("…"))

    @patch.object(agent, "_run_tool", return_value=[])
    @patch.object(agent, "_ask_deepseek")
    def test_fifth_tool_call_in_one_round_is_not_executed(self, ask_model, run_tool):
        calls = [
            {
                "id": "call-{}".format(index),
                "type": "function",
                "function": {
                    "name": "search_products",
                    "arguments": '{{"query":"Isabel {}"}}'.format(index),
                },
            }
            for index in range(5)
        ]
        decision = self._product_decision("no_match", "pestañas")
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)}
            if name == "set_turn_decision" else []
        )
        ask_model.side_effect = [
            {"content": "", "tool_calls": calls, "_fred_usage": {}},
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Te ayudo 😊", "_fred_usage": {}},
        ]
        result = agent.answer("Busco pestañas", history=[])
        search_calls = [call for call in run_tool.call_args_list if call.args[0] == "search_products"]
        self.assertEqual(len(search_calls), 4)
        self.assertIn("No encontré pestañas", result["reply"])

    @patch.object(agent, "_run_tool", return_value=[])
    @patch.object(agent, "_ask_deepseek")
    def test_duplicate_tool_call_is_not_executed_twice(self, ask_model, run_tool):
        repeated = {
            "type": "function",
            "function": {"name": "search_products", "arguments": '{"query":"Isabel"}'},
        }
        decision = self._product_decision("no_match", "Isabel")
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)}
            if name == "set_turn_decision" else []
        )
        ask_model.side_effect = [
            {
                "content": "",
                "tool_calls": [
                    {**repeated, "id": "call-one"},
                    {**repeated, "id": "call-two"},
                ],
                "_fred_usage": {},
            },
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Te ayudo 😊", "_fred_usage": {}},
        ]
        agent.answer("Busco Isabel", history=[])
        search_calls = [call for call in run_tool.call_args_list if call.args[0] == "search_products"]
        self.assertEqual(len(search_calls), 1)

    @patch.object(agent, "_ask_deepseek")
    def test_close_alternative_is_not_presented_as_exact_product(self, ask_model):
        decision = self._product_decision(
            "close_alternative",
            "perfume de Rare Beauty",
            "Rare Beauty Find Comfort Body & Hair Fragrance Mist",
            ["live_stock"],
            requested_type="perfume",
            matched_type="bruma corporal y capilar",
        )
        ask_model.side_effect = [
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Sí, tenemos perfume Rare Beauty.", "_fred_usage": {}},
        ]
        stock = {
            "found": True, "sku": "RB-MIST", "product_name": decision["matched_product"],
            "variant": "100 ml", "status": "in_stock", "quantity": 4, "price": "45000",
        }
        rag = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- Rare Beauty Find Comfort Body & Hair Fragrance Mist | variantes disponibles: "
            "100 ml | SKU: RB-MIST"
        )
        with patch.dict(agent.AVAILABLE_TOOLS, {"get_stock": lambda sku: stock}):
            result = agent.answer("¿Tienen perfume Rare Beauty?", rag_context=rag)

        self.assertIn("perfume de Rare Beauty como tal no encontré", result["reply"])
        self.assertIn("Body & Hair Fragrance Mist", result["reply"])
        self.assertNotIn("Sí, tenemos perfume Rare Beauty", result["reply"])
        self.assertEqual(result["decision"]["match_type"], "close_alternative")

    def test_exact_claim_is_downgraded_when_commercial_product_types_differ(self):
        decision = validate_model_decision(self._product_decision(
            "exact_match",
            "perfume de una marca",
            "Bruma corporal de la misma marca",
            requested_type="perfume",
            matched_type="bruma corporal",
        ))
        self.assertEqual(decision["match_type"], "close_alternative")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_product_decision_in_last_tool_round_is_rendered(self, ask_model, run_tool):
        searches = [
            {
                "content": "",
                "tool_calls": [{
                    "id": "search-{}".format(index),
                    "type": "function",
                    "function": {
                        "name": "search_products",
                        "arguments": '{{"query":"búsqueda {}"}}'.format(index),
                    },
                }],
                "_fred_usage": {},
            }
            for index in range(agent.MAX_TOOL_ROUNDS - 1)
        ]
        decision = self._product_decision(
            "close_alternative",
            "perfume de una marca",
            "Bruma corporal de la misma marca",
            requested_type="perfume",
            matched_type="bruma corporal",
        )
        ask_model.side_effect = searches + [{
            "content": "",
            "tool_calls": [self._decision_call(decision)],
            "_fred_usage": {},
        }]
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)}
            if name == "set_turn_decision" else []
        )

        result = agent.answer("¿Tienen perfume de esta marca?", history=[])

        self.assertEqual(result["decision"]["match_type"], "close_alternative")
        self.assertIn("como tal no encontré", result["reply"])
        self.assertNotIn("confirmás el nombre", result["reply"])

    @patch.object(agent, "_ask_deepseek")
    def test_related_accessory_does_not_satisfy_requested_product_type(self, ask_model):
        decision = self._product_decision(
            "same_brand_other_category",
            "pestañas",
            "Adhesivo resistente para pestañas",
            ["live_stock"],
            requested_type="pestañas",
            matched_type="adhesivo para pestañas",
        )
        ask_model.side_effect = [
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Lo único disponible es el adhesivo.", "_fred_usage": {}},
        ]
        stock = {
            "found": True, "sku": "GLUE-1", "product_name": decision["matched_product"],
            "variant": "default", "status": "in_stock", "quantity": 8, "price": "10000",
        }
        rag = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- Adhesivo resistente para pestañas | variantes disponibles: default | SKU: GLUE-1"
        )
        with patch.dict(agent.AVAILABLE_TOOLS, {"get_stock": lambda sku: stock}):
            result = agent.answer("¿Tienen pestañas?", rag_context=rag)

        self.assertIn("No encontré pestañas como tal", result["reply"])
        self.assertIn("no reemplaza lo que buscás", result["reply"])
        self.assertNotIn("Lo único disponible", result["reply"])

    @patch.object(agent, "_ask_deepseek")
    def test_explicit_price_request_for_verified_sku_runs_get_stock_and_answers_price(self, ask_model):
        decision = self._product_decision(
            "exact_match",
            "Labial X",
            "Labial X",
            ["live_stock", "live_price"],
            requested_type="labial",
            matched_type="labial",
        )
        ask_model.side_effect = [
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "¿Querés que te confirme el precio?", "_fred_usage": {}},
        ]
        stock_calls = []
        stock = {
            "found": True, "sku": "LIP-X", "product_name": "Labial X",
            "variant": "default", "status": "in_stock", "quantity": 3, "price": "30000",
        }
        def get_stock(sku):
            stock_calls.append(sku)
            return stock
        rag = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- Labial X | variantes disponibles: default | SKU: LIP-X"
        )
        with patch.dict(agent.AVAILABLE_TOOLS, {"get_stock": get_stock}):
            result = agent.answer("¿Tienen Labial X? ¿A qué precio?", rag_context=rag)

        self.assertEqual(stock_calls, ["LIP-X"])
        self.assertIn("$30.000", result["reply"])
        self.assertNotIn("querés que te confirme el precio", result["reply"].lower())
        self.assertIn("live_price", result["decision"]["checks_completed"])

    @patch.object(agent, "_ask_deepseek")
    def test_availability_check_does_not_erase_verified_price(self, ask_model):
        import json

        decision = self._product_decision(
            "close_alternative",
            "perfume de una marca",
            "Bruma corporal de la misma marca",
            ["live_stock", "live_price"],
            requested_type="perfume",
            matched_type="bruma corporal",
        )
        ask_model.side_effect = [
            {
                "content": "",
                "tool_calls": [{
                    "id": "availability-1",
                    "type": "function",
                    "function": {
                        "name": "get_product_availability",
                        "arguments": json.dumps({"product_id": 123}),
                    },
                }],
                "_fred_usage": {},
            },
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "texto libre que no debe gobernar", "_fred_usage": {}},
        ]
        stock = {
            "found": True, "sku": "MIST-1", "product_name": decision["matched_product"],
            "variant": "100 ml", "status": "in_stock", "quantity": 5, "price": "150000",
        }
        availability = {
            "found": True,
            "product_name": decision["matched_product"],
            "product_url": "https://example.com/mist",
            "variants": [{
                "sku": "MIST-1", "variant": "100 ml", "status": "in_stock", "quantity": 5,
            }],
        }
        rag = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- Bruma corporal de la misma marca | SKU: MIST-1"
        )
        with patch.dict(agent.AVAILABLE_TOOLS, {
            "get_stock": lambda sku: stock,
            "get_product_availability": lambda product_id: availability,
        }):
            result = agent.answer(
                "¿Tienen perfume de esta marca? ¿A qué precio?", rag_context=rag
            )

        self.assertIn("$150.000", result["reply"])
        self.assertNotIn("No pude confirmar el precio", result["reply"])

    @patch.object(agent, "_ask_deepseek")
    def test_exact_product_match_keeps_normal_information_path(self, ask_model):
        decision = self._product_decision(
            "exact_match", "Labial X", "Labial X", ["live_stock"]
        )
        ask_model.side_effect = [
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Sí, encontré Labial X.", "_fred_usage": {}},
        ]
        stock = {
            "found": True, "sku": "LIP-X", "product_name": "Labial X",
            "variant": "default", "status": "in_stock", "quantity": 3, "price": "30000",
        }
        rag = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- Labial X | variantes disponibles: default | SKU: LIP-X"
        )
        with patch.dict(agent.AVAILABLE_TOOLS, {"get_stock": lambda sku: stock}):
            result = agent.answer("¿Tienen Labial X?", rag_context=rag)

        self.assertIn("Sí, encontré Labial X", result["reply"])
        self.assertIn("Está disponible", result["reply"])
        self.assertNotIn("como tal no encontré", result["reply"])

    @patch.object(agent, "_ask_deepseek")
    def test_no_product_match_never_invents_and_asks_for_a_precise_reference(self, ask_model):
        decision = self._product_decision("no_match", "Producto inexistente")
        ask_model.side_effect = [
            {"content": "", "tool_calls": [self._decision_call(decision)], "_fred_usage": {}},
            {"content": "Sí, seguro lo tenemos.", "_fred_usage": {}},
        ]
        result = agent.answer("¿Tienen Producto inexistente?", rag_context="")

        self.assertIn("No encontré Producto inexistente publicado ahora", result["reply"])
        self.assertIn("nombre exacto o el link", result["reply"])
        self.assertNotIn("seguro lo tenemos", result["reply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
