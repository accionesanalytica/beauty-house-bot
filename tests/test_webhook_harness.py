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


def _default_fred_core_state(conversation_id):
    """A conversation Fred Core hasn't touched yet: CHAT, nothing else set.
    Individual tests override this per-test when a specific mode/active
    product matters to what's being asserted."""
    return {
        "mode": "CHAT", "active_product_id": None, "active_product_name": None,
        "active_sku": None, "active_variant": None, "unit_price": None,
        "quantity": None, "delivery_method": None, "customer_name": None,
        "customer_email": None, "postal_code": None, "checkout_step": None,
        "order_number": None,
    }


@patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0)
@patch.object(app, "get_fred_core_state", _default_fred_core_state)
@patch.object(app, "save_fred_core_state", lambda *args, **kwargs: None)
@patch.object(app, "reset_fred_core_checkout", lambda conversation_id: None)
# Fred Core's CHAT-mode migration safety net (an active legacy sales_intake
# forces mode=CHECKOUT) reads get_active_sales_intake on every turn; default
# it to "no legacy intake" here so tests stay offline unless a test
# explicitly overrides it.
@patch.object(app, "get_active_sales_intake", lambda conversation_id: None)
class WebhookHarnessTests(unittest.TestCase):
    PHONE = "5491111111111"

    def _post(self, text, message_id="wamid-test"):
        return asyncio.run(app.webhook_post(IncomingRequest(self.PHONE, text, message_id)))

    def _post_button(self, button_id, title, message_id="wamid-button"):
        """A real interactive button reply, the only way a sensitive action
        can start now."""
        class _ButtonRequest:
            def __init__(self, phone):
                self._body = {"entry": [{"changes": [{"value": {"messages": [{
                    "from": phone, "id": message_id,
                    "interactive": {"button_reply": {"id": button_id, "title": title}},
                }]}}]}]}

            async def json(self):
                return self._body

        return asyncio.run(app.webhook_post(_ButtonRequest(self.PHONE)))

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

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "get_product_selection", return_value=None)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_ungrounded_answer_hands_over_isa_contact_and_never_a_menu(
        self, history, inbound, get_selection, send_message, record_message, record_turn
    ):
        agent_result = {
            "reply": "No encontré todavía una opción que pueda recomendarte con confianza.",
            "tool_calls": [], "usage": {}, "graceful_fallback_tier": "escalate",
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        with patch.object(app, "search_similar_products", return_value=""), patch.object(
            app, "answer", return_value=agent_result
        ):
            response = self._post("no sé qué elegir", "wamid-escalate")

        self.assertEqual(response.status_code, 200)
        delivered = send_message.call_args.args[1]
        self.assertNotIn("¿Cómo querés seguir?", delivered)
        self.assertIn(app.isa_contact_number(), delivered)

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_generic_hedge_reply_is_not_turned_into_a_menu(
        self, history, inbound, send_message, record_message, record_turn
    ):
        agent_result = {
            "reply": "Mirá, eso no lo tengo confirmado de forma segura ahora mismo.",
            "tool_calls": [], "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        # A `with`-block context manager (not a decorator) so this override
        # actually wins over the class-level default: mock.patch's class
        # decorator support takes precedence over a same-target *method*
        # decorator, but not over a context manager entered from inside the
        # test body.
        with patch.object(app, "search_similar_products", return_value=""), patch.object(
            app, "answer", return_value=agent_result
        ), patch.object(app, "get_fred_core_state", return_value={
            **_default_fred_core_state(7), "active_product_name": "SHOOW TOOLS - ISABEL I",
        }):
            response = self._post("¿Puedo pagar en efectivo al recibir el envío?", "wamid-hedge")

        self.assertEqual(response.status_code, 200)
        # A hedge no longer becomes a menu: the answer stands as written.
        delivered = send_message.call_args.args[1]
        self.assertEqual(delivered, agent_result["reply"])
        self.assertNotIn("¿Cómo querés seguir?", delivered)

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
    def test_direct_human_request_hands_over_isa_contact(
        self, history, inbound, queue_for_isa, send_message, record_message
    ):
        # No case, no consultation, no notification: just her number.
        with patch.object(app, "answer") as ask_model, patch.object(
            app, "search_similar_products",
        ) as retrieve:
            response = self._post("Pasame con Isa", "wamid-handoff")

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        retrieve.assert_not_called()
        queue_for_isa.assert_not_called()
        delivered = send_message.call_args.args[1]
        self.assertIn("Isa", delivered)
        self.assertIn(app.isa_contact_number(), delivered)

    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    def test_the_buy_button_opens_the_checkout_on_exactly_that_sku(
        self, history, inbound, send_message, record_message
    ):
        # The click fixed the product. The checkout must use that SKU and
        # never re-discover what the customer "meant".
        live_stock = {
            "found": True, "status": "in_stock", "sku": "3D24A",
            "product_name": "AOA STUDIO - PEGA DE PESTAÑAS COREANA",
            "variant": "", "price": "18000.00",
        }
        with patch.object(app, "get_stock", return_value=live_stock) as get_stock, patch.object(
            app, "_start_sales_intake", return_value="pedir datos",
        ) as start_intake, patch.object(app, "answer") as ask_model:
            response = self._post_button(
                "{}3D24A".format(app.BUY_BUTTON_PREFIX), "Comprar", "wamid-buy-click",
            )

        self.assertEqual(response.status_code, 200)
        ask_model.assert_not_called()
        get_stock.assert_called_once_with("3D24A")
        self.assertEqual(start_intake.call_args.args[1]["sku"], "3D24A")
        self.assertEqual(
            start_intake.call_args.args[1]["product_name"],
            "AOA STUDIO - PEGA DE PESTAÑAS COREANA",
        )

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

    def test_lifting_requires_a_model_or_link_without_calling_the_model(self):
        reply = app._lifting_clarification_reply("Tengo lifting, ¿qué pestañas me recomendás?")
        self.assertIn("nombre exacto", reply)
        self.assertIn("link", reply)
        self.assertEqual(app._lifting_clarification_reply("Busco pestañas naturales"), "")

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_lifting_enters_knowledge_flow_and_enforces_approved_reference(
        self, history, inbound, send_message, record_message, record_turn
    ):
        from knowledge_rag import KnowledgeObligations, KnowledgeRetrieval

        obligations = KnowledgeObligations(
            topics=("lashes_guidance",),
            required_disclosures=({
                "id": "lifting-band", "text": "Con lifting no se recomienda banda completa.",
            },),
            required_links=({
                "id": "lifting-link", "link_type": "approved_static_link",
                "url": "https://www.instagram.com/p/DZ3U5VGtnrX/",
            },),
        )
        retrieval = KnowledgeRetrieval(
            rows=({"source_id": "lifting", "content": "Taylor cluster"},),
            context="Conocimiento aprobado: Taylor cluster; no banda completa.",
            obligations=obligations,
            retrieved_topics=("lashes_guidance",),
            governing_topic="lashes_guidance",
        )
        result = {
            "reply": "Para lifting podés evaluar Taylor cluster.",
            "tool_calls": [],
            "decision": {"action": "reply", "reason": "normal_response"},
            "usage": {},
            "model_calls": 1,
        }
        with patch.object(app, "KNOWLEDGE_RAG_ENABLED", True), patch.object(
            app, "embed_text", return_value=[0.1]
        ), patch.object(
            app, "search_similar_products", return_value=""
        ), patch.object(
            app, "search_knowledge_bundle", return_value=retrieval
        ) as knowledge, patch.object(app, "answer", return_value=result) as ask_model:
            response = self._post(
                "Tengo lifting, ¿qué pestañas me recomendás?", "wamid-lifting-knowledge"
            )

        self.assertEqual(response.status_code, 200)
        knowledge.assert_called_once()
        ask_model.assert_called_once()
        visible = send_message.call_args.args[1]
        self.assertIn("no se recomienda banda completa", visible)
        self.assertIn("https://www.instagram.com/p/DZ3U5VGtnrX/", visible)

    def test_isa_policy_instruction_is_translated_not_forwarded_verbatim(self):
        reply = app._isa_customer_instruction(
            "mandar políticas en pdf",
            {"action_type": "human_handoff"},
        )
        self.assertIn("beautyhousemakeup.com/politicas", reply)
        self.assertNotIn("mandar políticas", reply)

    @patch.object(app, "get_product_availability")
    @patch.object(app, "search_available_products")
    def test_chocolate_lashes_use_live_filtered_candidates(self, search_available, availability):
        """A common colour must not make Fred recommend lip or brow products."""
        search_available.return_value = [
            {"product_id": 11, "name": "ISABEL I (CHOCOLATE)"},
            {"product_id": 12, "name": "TAYLOR (CHOCOLATE)"},
            {"product_id": 13, "name": "Pomada de cejas Chocolate"},
        ]
        availability.side_effect = [
            {
                "found": True,
                "product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
                "description": "Pestañas de banda con acabado natural.",
                "variants": [{"variant": "8/8/10/12 mm", "status": "in_stock"}],
            },
            {
                "found": True,
                "product_name": "SHOOW TOOLS - TAYLOR (CHOCOLATE)",
                "description": "Pestañas de banda suaves para uso diario.",
                "variants": [{"variant": "8/8/10/12 mm", "status": "in_stock"}],
            },
            {
                "found": True,
                "product_name": "Pomada de cejas Chocolate",
                "description": "Producto para cejas.",
                "variants": [{"variant": "Única", "status": "in_stock"}],
            },
        ]

        context = app._live_candidate_context(
            "Productos recuperados: Pomada de cejas.",
            "Busco pestañas naturales para todos los días, en chocolate.",
        )

        search_available.assert_called_once_with("chocolate", limit=10)
        self.assertIn("ISABEL I", context)
        self.assertIn("TAYLOR", context)
        self.assertNotIn("Pomada", context)
        self.assertIn("tienen stock positivo", context)

    def test_verified_chocolate_lashes_cannot_fall_back_to_no_stock_copy(self):
        live_context = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas: "
            "estas opciones tienen stock positivo ahora.\n"
            "- SHOOW TOOLS - ISABEL I (CHOCOLATE) | variantes disponibles: 8/8/10/12 mm "
            "| SKU: ISABEL-CHOCO | Link: https://beautyhousemakeup.com/productos/isabel/\n"
            "- SHOOW TOOLS - TAYLOR (CHOCOLATE) | variantes disponibles: 8/8/10/12 mm "
            "| SKU: TAYLOR-CHOCO | Link: https://beautyhousemakeup.com/productos/taylor/"
        )
        reply = app._grounded_lash_recommendation(
            live_context,
            "Busco pestañas naturales para todos los días. Si hay chocolate, mejor.",
        )

        self.assertIn("Isabel I (chocolate)", reply)
        self.assertIn("Taylor (chocolate)", reply)
        self.assertIn("https://beautyhousemakeup.com/productos/isabel/", reply)
        self.assertIn("https://beautyhousemakeup.com/productos/taylor/", reply)
        self.assertNotIn("no tengo nada", reply.lower())
        self.assertEqual(
            app._grounded_lash_recommendation(live_context, "Quiero comprar Isabel I chocolate"),
            "",
        )

    @patch.object(app, "get_stock")
    def test_confirmed_named_choice_bypasses_recommendation_and_starts_sale(self, get_stock):
        live_context = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- SHOOW TOOLS - ISABEL I (CHOCOLATE) | variantes disponibles: 8/8/10/12 mm "
            "| SKU: ISABEL-CHOCO\n"
            "- SHOOW TOOLS - TAYLOR (CHOCOLATE) | variantes disponibles: 8/8/10/12 mm "
            "| SKU: TAYLOR-CHOCO"
        )
        get_stock.return_value = {
            "status": "in_stock",
            "sku": "ISABEL-CHOCO",
            "product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
            "variant": "8/8/10/12 mm",
            "price": "30000",
        }

        candidate = app._live_purchase_candidate(
            live_context,
            "Me quedo con Isabel I chocolate. Quiero 2 unidades, envío. Nombre: Luis Vera. Email: luis@example.com",
        )

        self.assertEqual(candidate["sku"], "ISABEL-CHOCO")
        self.assertEqual(candidate["unit_price"], "30000")
        get_stock.assert_called_once_with("ISABEL-CHOCO")

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    @patch.object(app, "get_active_sales_intake", return_value=None)
    def test_purchase_message_outranks_recommendation_card(
        self, active_intake, history, inbound, send_message, record_message, record_turn
    ):
        live_context = (
            "Disponibilidad Tiendanube verificada para candidatas recuperadas:\n"
            "- SHOOW TOOLS - ISABEL I (CHOCOLATE) | variantes disponibles: 8/8/10/12 mm "
            "| SKU: ISABEL-CHOCO"
        )
        stock = {
            "found": True, "status": "in_stock", "sku": "ISABEL-CHOCO",
            "product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
            "variant": "8/8/10/12 mm", "price": "30000",
        }
        agent_result = {
            "reply": "Dale, te confirmo Isabel I chocolate 😊",
            "tool_calls": [], "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        with patch.object(app, "search_similar_products", return_value="Productos encontrados"), patch.object(
            app, "_live_candidate_context", return_value=live_context
        ), patch.object(app, "get_stock", return_value=stock), patch.object(
            app, "_start_sales_intake"
        ) as start_intake, patch.object(
            app, "answer", return_value=agent_result
        ), patch.object(app, "send_customer_action_buttons", return_value=True) as action_buttons:
            response = self._post(
                "Me quedo con Isabel I chocolate. Quiero 2 unidades, envío. "
                "Nombre: Luis Vera. Email: luis@example.com",
                "wamid-direct-purchase",
            )

        self.assertEqual(response.status_code, 200)
        # Writing a purchase no longer opens a checkout. It stays a
        # conversation, and Fred offers a Comprar button carrying the real SKU.
        start_intake.assert_not_called()
        buy_ids = [
            b["id"] for b in action_buttons.call_args.args[2]
            if b["id"].startswith(app.BUY_BUTTON_PREFIX)
        ]
        self.assertEqual(buy_ids, ["{}ISABEL-CHOCO".format(app.BUY_BUTTON_PREFIX)])

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    @patch.object(app, "get_active_sales_intake", return_value=None)
    def test_purchase_details_use_fred_core_active_product_without_model(
        self, active_intake, history, inbound, send_message, record_message, record_turn
    ):
        # Fred Core's active_product -- not conversation_product_selections
        # -- is what a purchase-intent message resolves against now.
        fred_core_state = {
            "mode": "CHAT", "active_product_id": "ISABEL-CHOCO",
            "active_product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
            "active_sku": "ISABEL-CHOCO", "active_variant": "8/8/10/12 mm",
            "unit_price": "30000", "quantity": None, "delivery_method": None,
            "customer_name": None, "customer_email": None, "postal_code": None,
            "checkout_step": None, "order_number": None,
        }
        live_stock = {
            "found": True, "status": "in_stock", "sku": "ISABEL-CHOCO",
            "product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
            "variant": "8/8/10/12 mm", "price": "30000",
        }
        with patch.object(app, "search_similar_products", return_value=""), patch.object(
            app, "_live_candidate_context", return_value=""
        ), patch.object(app, "get_fred_core_state", return_value=fred_core_state), patch.object(
            app, "get_stock", return_value=live_stock
        ), patch.object(app, "_start_sales_intake") as start_intake, patch.object(
            app, "send_customer_action_buttons", return_value=True,
        ) as action_buttons, patch.object(app, "answer", return_value={
            "reply": "Dale, tomo nota 😊", "tool_calls": [], "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }):
            response = self._post(
                "Quiero 2 unidades, envío. Nombre: Luis Vera. Email: luis@example.com",
                "wamid-previous-selection",
            )

        self.assertEqual(response.status_code, 200)
        # The active product is still remembered and still what the Comprar
        # button would buy -- but it no longer opens a checkout by itself.
        start_intake.assert_not_called()
        buy_ids = [
            b["id"] for b in action_buttons.call_args.args[2]
            if b["id"].startswith(app.BUY_BUTTON_PREFIX)
        ]
        self.assertEqual(buy_ids, ["{}ISABEL-CHOCO".format(app.BUY_BUTTON_PREFIX)])

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    @patch.object(app, "get_active_sales_intake", return_value=None)
    def test_model_purchase_handoff_cannot_open_blank_sale_form(
        self, active_intake, history, inbound, send_message, record_message, record_turn
    ):
        result = {
            "reply": "Voy a preparar la compra.", "tool_calls": [], "usage": {},
            "handoff": {"reason": "purchase_intent", "summary": "Quiere comprar."},
            "decision": {"action": "start_sales_intake", "reason": "purchase_intent"},
        }
        with patch.object(app, "search_similar_products", return_value=""), patch.object(
            app, "_live_candidate_context", return_value=""
        ), patch.object(app, "get_product_selection", return_value=None), patch.object(
            app, "_start_sales_intake"
        ) as start_intake, patch.object(app, "answer", return_value=result):
            response = self._post("Quiero comprar", "wamid-no-product")

        self.assertEqual(response.status_code, 200)
        start_intake.assert_not_called()
        self.assertIn("cuál producto", send_message.call_args.args[1])

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    def test_clean_unverified_product_query_searches_before_any_sales_form(
        self, history, inbound, send_message, record_message, record_turn
    ):
        agent_result = {
            "reply": (
                "No encuentro un perfume de Rare Beauty publicado ahora. "
                "¿Tenés el nombre exacto para que pueda verificarlo?"
            ),
            "tool_calls": [],
            "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        with patch.object(app, "get_active_sales_intake", return_value=None), patch.object(
            app, "search_similar_products", return_value=""
        ) as retrieve, patch.object(
            app, "_live_candidate_context", return_value=""
        ), patch.object(
            app, "get_product_selection", return_value=None
        ), patch.object(
            app, "_start_sales_intake"
        ) as start_intake, patch.object(
            app, "answer", return_value=agent_result
        ) as ask_model:
            response = self._post(
                "Me gustaría comprar un perfume de Rare Beauty, ¿tendrán? ¿a qué precio?",
                "wamid-clean-unverified-product",
            )

        self.assertEqual(response.status_code, 200)
        retrieve.assert_called_once()
        ask_model.assert_called_once()
        start_intake.assert_not_called()
        delivered = send_message.call_args.args[1]
        self.assertEqual(delivered, agent_result["reply"])
        for forbidden in (
            "cuántas unidades", "nombre y apellido", "email:",
            "checkout", "link de pago", "para dejarlo listo",
        ):
            self.assertNotIn(forbidden, delivered.lower())

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
    @patch.object(app, "SALES_INTAKE_ENABLED", True)
    def test_new_product_query_escapes_old_intake_and_reaches_retrieval(
        self, history, inbound, send_message, record_message, record_turn
    ):
        old_intake = {
            "status": "quantity",
            "product_request": "Isabel I chocolate",
            "selected_sku": "ISABEL-CHOCO",
            "selected_variant": "8/8/10/12 mm",
            "unit_price": "30000",
            "quantity": None,
            "fulfillment": None,
            "customer_name": None,
            "customer_email": None,
        }
        agent_result = {
            "reply": (
                "No encuentro un perfume de Rare Beauty publicado. "
                "Pasame el nombre, una foto o un link y reviso si puede pedirse por encargo."
            ),
            "tool_calls": [],
            "usage": {},
            "decision": {"action": "reply", "reason": "normal_response"},
        }
        with patch.object(app, "get_active_sales_intake", return_value=old_intake), patch.object(
            app, "cancel_sales_intake"
        ) as cancel_intake, patch.object(
            app, "clear_product_selection"
        ) as clear_selection, patch.object(
            app, "search_similar_products", return_value=""
        ) as retrieve, patch.object(
            app, "_live_candidate_context", return_value=""
        ), patch.object(
            app, "get_product_selection", return_value=None
        ), patch.object(
            app, "answer", return_value=agent_result
        ) as ask_model:
            response = self._post(
                "Me gustaría comprar un perfume de Rare Beauty, ¿tendrán? ¿A qué precio?",
                "wamid-new-product-after-old-intake",
            )

        self.assertEqual(response.status_code, 200)
        cancel_intake.assert_called_once_with(7)
        clear_selection.assert_called_once_with(7)
        retrieve.assert_called_once()
        ask_model.assert_called_once()
        send_message.assert_called_once_with(self.PHONE, agent_result["reply"])

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    @patch.object(app, "KNOWLEDGE_RAG_ENABLED", True)
    def test_embedding_outage_falls_back_to_lexical_catalog(
        self, history, inbound, send_message, record_message, record_turn
    ):
        """Knowledge RAG must not turn a catalog answer into an empty answer."""
        agent_result = {"reply": "Sí, encontré opciones 😊", "tool_calls": [], "usage": {}}
        with patch.object(app, "embed_text", side_effect=RuntimeError("unavailable")), patch.object(
            app, "search_similar_products", return_value="Productos encontrados: Isabel I"
        ) as retrieve, patch.object(
            app, "_live_candidate_context", return_value="Disponibilidad Tiendanube verificada"
        ), patch.object(app, "answer", return_value=agent_result):
            response = self._post("Busco pestañas chocolate", "wamid-embedding-outage")

        self.assertEqual(response.status_code, 200)
        retrieve.assert_called_once_with("Busco pestañas chocolate")
        send_message.assert_called_once_with(self.PHONE, agent_result["reply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
