import json
import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(ROOT, "bot")
EVAL_DIR = os.path.join(ROOT, "evals")
for path in (BOT_DIR, EVAL_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from fred_v2_ab import compare_turns  # noqa: E402
from v2_agent import FredV2Agent  # noqa: E402
from v2_tools import TOOL_SCHEMAS, V2ToolAdapters, live_handoff_adapter  # noqa: E402


def model_tool(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


class ScriptedModel:
    def __init__(self, messages):
        self.messages = list(messages)
        self.seen = []

    def __call__(self, messages):
        self.seen.append(messages)
        return self.messages.pop(0)


class FredV2VerticalSliceTests(unittest.TestCase):
    def setUp(self):
        self.called = []
        self.tools = V2ToolAdapters(
            knowledge_search=lambda query: self._record(
                "search_knowledge", query,
                {"found": True, "context": "El showroom atiende con coordinación previa."},
            ),
            order_lookup=lambda number: self._record(
                "get_order", number,
                {
                    "found": True,
                    "order_number": number,
                    "fulfillment_status": "PACKED",
                    "shipping_type": "pickup",
                    "tracking": None,
                },
            ),
            product_lookup=lambda query: self._record(
                "get_product", query,
                {
                    "found": True,
                    "products": [{
                        "product_name": "SHOOW TOOLS - ISABEL I",
                        "variants": [{
                            "sku": "ISABEL-I-CHOCOLATE",
                            "variant": "Chocolate",
                            "status": "in_stock",
                            "quantity": 4,
                        }],
                    }],
                },
            ),
            handoff=lambda payload: self._record(
                "handoff_to_isa", payload,
                {"accepted": True, "mode": "preview", **payload},
            ),
        )

    def _record(self, name, arguments, result):
        self.called.append((name, arguments))
        return result

    def run_script(self, script, message, history=None):
        model = ScriptedModel(script)
        result = FredV2Agent(model_call=model, tools=self.tools).answer(
            message, history=history,
        )
        return result, model

    def test_hooolaaa_is_natural_and_uses_no_tools(self):
        result, _ = self.run_script(
            [{"content": "¡Hooolaaa! 😊 ¿Cómo te puedo ayudar?"}], "Hooolaaa",
        )
        self.assertEqual([], result["tool_calls"])
        self.assertEqual(1, result["model_calls"])

    def test_hello_there_is_natural_and_uses_no_tools(self):
        result, _ = self.run_script(
            [{"content": "¡Hello! 😊 ¿En qué te ayudo?"}], "Hello there",
        )
        self.assertEqual([], result["tool_calls"])

    def test_showroom_uses_knowledge(self):
        result, model = self.run_script([
            model_tool("search_knowledge", {"query": "pasar por el showroom"}),
            {"content": "Podés pasar por el showroom coordinando previamente 😊"},
        ], "quiero pasar por el showroom")
        self.assertEqual(["search_knowledge"], [call["name"] for call in result["tool_calls"]])
        self.assertIn("showroom", model.seen[-1][-1]["content"])

    def test_order_question_without_number_asks_for_it_without_tools(self):
        result, _ = self.run_script(
            [{"content": "Claro, ¿me pasás el número de pedido?"}],
            "quiero saber dónde está mi pedido",
        )
        self.assertEqual([], result["tool_calls"])
        self.assertIn("número", result["reply"])

    def test_followup_number_uses_order_fulfillment(self):
        history = [
            {"role": "user", "content": "quiero saber dónde está mi pedido"},
            {"role": "assistant", "content": "¿Me pasás el número de pedido?"},
        ]
        result, model = self.run_script([
            model_tool("get_order", {"order_number": "6344"}),
            {"content": "El pedido #6344 está preparado para retiro."},
        ], "6344", history=history)
        self.assertEqual([("get_order", "6344")], self.called)
        tool_evidence = json.loads(model.seen[-1][-1]["content"])
        self.assertEqual("PACKED", tool_evidence["fulfillment_status"])
        self.assertEqual("pickup", tool_evidence["shipping_type"])
        self.assertEqual("get_order", result["tool_results"][0]["name"])

    def test_packed_pickup_reply_is_fixed_by_adapter_not_model(self):
        result, _ = self.run_script([
            model_tool("get_order", {"order_number": "6342"}),
            {"content": "Ya está listo para retirar."},
        ], "pedido 6342")
        self.assertIn("empaquetado", result["reply"].lower())
        self.assertIn("confirmación", result["reply"].lower())
        self.assertNotIn("listo para retirar", result["reply"].lower())
        evidence = result["tool_results"][0]["result"]
        self.assertEqual("packed_waiting_pickup_confirmation", evidence["fulfillment_semantics"])

    def test_specific_product_uses_get_product(self):
        result, _ = self.run_script([
            model_tool("get_product", {"query": "Isabel I Chocolate"}),
            {"content": "Sí, Isabel I Chocolate figura disponible."},
        ], "¿Tienen Isabel I Chocolate?")
        self.assertEqual(["get_product"], [call["name"] for call in result["tool_calls"]])
        self.assertEqual("get_product", self.called[0][0])

    def test_quantity_plus_product_hands_off_without_checkout(self):
        result, _ = self.run_script([
            model_tool("handoff_to_isa", {
                "reason": "purchase_intent",
                "summary": "Quiere 4 Isabel I Chocolate.",
            }),
            {"content": "¡Genial! Isa puede ayudarte a coordinar la compra."},
        ], "quiero 4 Isabel I Chocolate")
        self.assertEqual(["handoff_to_isa"], [call["name"] for call in result["tool_calls"]])
        self.assertNotIn("checkout", json.dumps(self.called))

    def test_vague_advice_hands_off(self):
        result, _ = self.run_script([
            model_tool("handoff_to_isa", {
                "reason": "product_advice",
                "summary": "Busca pestañas naturales y necesita asesoramiento.",
            }),
            {"content": "Para recomendarte bien, Isa puede ayudarte 😊"},
        ], "quiero unas pestañas naturales")
        self.assertEqual("product_advice", result["tool_calls"][0]["arguments"]["reason"])

    def test_not_found_structurally_requires_custom_order_handoff(self):
        tools = V2ToolAdapters(
            product_lookup=lambda query: {"found": False, "products": [], "query": query},
            handoff=lambda payload: {
                "status": "simulated_success", "would_handoff": True,
                "side_effect_executed": False, **payload,
            },
        )
        model = ScriptedModel([
            model_tool("get_product", {"query": "Modelo Fantasma Ultra"}),
            {"content": "No aparece publicado."},
            model_tool("handoff_to_isa", {
                "reason": "custom_order", "summary": "Producto no encontrado; revisar encargo.",
            }),
            {"content": "No aparece en la web; Isa puede revisar si se consigue por encargo."},
        ])
        result = FredV2Agent(model_call=model, tools=tools).answer(
            "tienen las pestañas Modelo Fantasma Ultra?",
        )
        self.assertEqual(["get_product", "handoff_to_isa"], [
            call["name"] for call in result["tool_calls"]
        ])
        self.assertEqual("custom_order", result["tool_calls"][-1]["arguments"]["reason"])
        self.assertTrue(result["tool_results"][-1]["result"]["would_handoff"])
        self.assertFalse(result["tool_results"][-1]["result"]["side_effect_executed"])
        self.assertIn("encargo", result["reply"].lower())
        self.assertNotIn("similar", result["reply"].lower())

    def test_current_turn_is_explicitly_separated_from_old_context(self):
        result, model = self.run_script(
            [{"content": "¡Hola de nuevo! ¿En qué te ayudo?"}],
            "hola", history=[
                {"role": "user", "content": "¿Tienen Isabel I?"},
                {"role": "assistant", "content": "Sí, figura publicada."},
            ],
        )
        sent = model.seen[0]
        self.assertEqual("system", sent[-2]["role"])
        self.assertIn("CURRENT TURN", sent[-2]["content"])
        self.assertEqual({"role": "user", "content": "hola"}, sent[-1])
        self.assertEqual([], result["tool_calls"])

    def test_existing_visual_identification_case_requests_evidence_without_tools(self):
        result, _ = self.run_script(
            [{"content": "¿Tenés una foto, link o sabés el nombre del modelo?"}],
            "eran como marrones", history=[
                {"role": "user", "content": "Vi unas pestañas pero no recuerdo el nombre"},
                {"role": "assistant", "content": "¿Tenés una foto o link?"},
            ],
        )
        self.assertEqual([], result["tool_calls"])
        self.assertIn("foto", result["reply"].lower())

    def test_existing_social_acknowledgement_does_not_reuse_showroom_topic(self):
        result, _ = self.run_script(
            [{"content": "¡Dale, perfecto! 😊"}],
            "dale perfecto", history=[
                {"role": "user", "content": "¿Puedo pasar por el showroom?"},
                {"role": "assistant", "content": "Sí, coordinando previamente."},
            ],
        )
        self.assertEqual([], result["tool_calls"])


class ClosedToolContractTests(unittest.TestCase):
    def test_only_four_tools_are_exposed(self):
        names = {item["function"]["name"] for item in TOOL_SCHEMAS}
        self.assertEqual(
            {"search_knowledge", "get_order", "get_product", "handoff_to_isa"}, names,
        )

    def test_unknown_tool_and_invalid_order_are_blocked(self):
        tools = V2ToolAdapters()
        with self.assertRaises(ValueError):
            tools.call("create_checkout", {})
        with self.assertRaises(ValueError):
            tools.call("get_order", {"order_number": "6344; DROP"})

    def test_live_handoff_reuses_existing_queue_only_when_called(self):
        adapter = live_handoff_adapter(
            conversation_id=7,
            customer_phone="54911",
            customer_message="quiero cuatro",
            conversation_context=[],
        )
        self.assertTrue(callable(adapter))

    def test_default_handoff_is_structured_simulation_without_side_effect(self):
        result = V2ToolAdapters().call("handoff_to_isa", {
            "reason": "product_advice", "summary": "Necesita recomendación.",
        })
        self.assertEqual("simulated_success", result["status"])
        self.assertTrue(result["would_handoff"])
        self.assertFalse(result["side_effect_executed"])

    def test_packed_shipping_is_normalised_as_waiting_dispatch(self):
        tools = V2ToolAdapters(order_lookup=lambda number: {
            "found": True, "order_number": number, "fulfillment_status": "PACKED",
            "shipping_type": "ship", "tracking": None, "carrier": None,
        })
        result = tools.call("get_order", {"order_number": "6342"})
        self.assertEqual("packed_waiting_dispatch", result["fulfillment_semantics"])
        self.assertIn("empaquetado", result["customer_safe_reply"].lower())
        self.assertNotIn("listo para retirar", result["customer_safe_reply"].lower())


class ABHarnessTests(unittest.TestCase):
    def test_same_messages_and_separate_histories_are_compared(self):
        def runner(label):
            def run(message, history):
                return {
                    "reply": "{}:{}:{}".format(label, len(history), message),
                    "tool_calls": [{"name": "search_knowledge", "arguments": {}}],
                    "model_calls": 1,
                    "errors": [],
                }
            return run

        rows = compare_turns(
            ["hola", "showroom"], v1_runner=runner("v1"), v2_runner=runner("v2"),
            hallucination_check=lambda message, result: ["review"] if message == "showroom" else [],
        )
        self.assertEqual("v1:0:hola", rows[0]["v1"]["reply"])
        self.assertEqual("v2:2:showroom", rows[1]["v2"]["reply"])
        self.assertEqual(["search_knowledge"], rows[1]["v2"]["tools"])
        self.assertEqual(["review"], rows[1]["v2"]["hallucinations"])
        self.assertIn("latency_ms", rows[1]["v2"])


if __name__ == "__main__":
    unittest.main()
