import asyncio
import inspect
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_DIR = os.path.join(ROOT, "bot")
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

import app  # noqa: E402
import conversation_store  # noqa: E402
import operations_store  # noqa: E402
import v2_runtime  # noqa: E402
import v2_live_adapters  # noqa: E402
from v2_agent import FredV2Agent, V2AgentExecutionError  # noqa: E402
from v2_live_adapters import ISA_PUBLIC_DISPLAY, live_handoff_adapter  # noqa: E402
from v2_tools import V2ToolAdapters  # noqa: E402


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
        self.seen.append(list(messages))
        return self.messages.pop(0)


class V2HandoffTests(unittest.TestCase):
    def setUp(self):
        self.isa_env = patch.dict(os.environ, {"ISA_WHATSAPP_NUMBER": "5491124528750"})
        self.isa_env.start()
        self.addCleanup(self.isa_env.stop)

    def _adapter(self, *, pending=(), created=None):
        return live_handoff_adapter(
            conversation_id=7,
            correlation_id="correlation-7",
            pending_handoffs=pending,
            create_handoff=created or (lambda **kwargs: {"id": 31, "status": "prepared"}),
        )

    def test_live_adapter_contains_no_isa_queue_or_sender(self):
        source = inspect.getsource(v2_live_adapters)
        self.assertNotIn("_queue_for_isa", source)
        self.assertNotIn("send_whatsapp", source)
        self.assertNotIn("requests.post", source)

    def test_create_handoff_generates_exact_number_and_dynamic_wa_link(self):
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return {"id": 31, "status": "prepared"}

        result = self._adapter(created=create)({
            "operation": "create",
            "reason": "cancel_order",
            "customer_reason": "cancelar pedido",
            "order_number": "6344",
            "summary": (
                "quiero cancelar el pedido #6344 y Fred me indicó que este tema "
                "necesita tu intervención."
            ),
        })

        parsed = urlparse(result["wa_url"])
        message = parse_qs(parsed.query)["text"][0]
        self.assertEqual("wa.me", parsed.netloc)
        self.assertEqual("/5491124528750", parsed.path)
        self.assertEqual(
            "Hola Isa, Fred me recomendó hablar con vos.\n\n"
            "Motivo: cancelar pedido\n"
            "Pedido: #6344\n"
            "Contexto: quiero cancelar el pedido #6344 y Fred me indicó que este tema "
            "necesita tu intervención.",
            message,
        )
        self.assertIn(ISA_PUBLIC_DISPLAY, result["customer_safe_reply"])
        self.assertFalse(result["message_sent_to_isa"])
        self.assertEqual("prepared", captured.get("status", "prepared"))

    def test_handoff_storage_excludes_free_text_pii(self):
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return {"id": 31, "status": "prepared"}

        result = self._adapter(created=create)({
            "reason": "human_request",
            "summary": (
                "Soy Ana Pérez, vivo en Calle Falsa 123; escribime a ana@example.com "
                "o +54 9 11 9999-8888. DNI 12.345.678."
            ),
        })
        self.assertEqual(
            {"correlation_id", "conversation_id", "reason", "order_number"},
            set(captured),
        )
        rendered = json.dumps({"stored": captured, "url": result["wa_url"]})
        for private_value in (
            "Ana Pérez", "Calle Falsa", "ana@example.com", "9999-8888", "12.345.678",
        ):
            self.assertNotIn(private_value, rendered)

    def test_retry_link_uses_canonical_state_returned_by_storage(self):
        def create(**kwargs):
            return {
                "id": 31,
                "status": "pending",
                "reason": "cancel_order",
                "order_number": "6344",
            }

        result = self._adapter(created=create)({
            "operation": "create",
            "reason": "modify_order",
            "customer_reason": "CAMBIO DE RETRY",
            "order_number": "9999",
            "summary": "Contexto distinto del retry.",
        })
        message = parse_qs(urlparse(result["wa_url"]).query)["text"][0]
        self.assertIn("Motivo: cancelar pedido", message)
        self.assertIn("Pedido: #6344", message)
        self.assertIn(
            "Contexto: quiero cancelar el pedido #6344 y Fred me indicó que este tema "
            "necesita tu intervención.",
            message,
        )
        self.assertNotIn("CAMBIO DE RETRY", message)
        self.assertNotIn("9999", message)

    def test_second_create_in_same_turn_reuses_first_canonical_handoff(self):
        create = MagicMock(return_value={
            "id": 31, "status": "prepared", "reason": "cancel_order",
            "order_number": "6344",
        })
        adapter = self._adapter(created=create)
        first = adapter({
            "operation": "create", "reason": "cancel_order",
            "order_number": "6344",
        })
        second = adapter({
            "operation": "create", "reason": "purchase_intent",
        })
        create.assert_called_once()
        self.assertTrue(second["reused_existing"])
        self.assertEqual(first["wa_url"], second["wa_url"])
        self.assertEqual("cancel_order", second["reason"])

    def test_safe_handoff_copy_overrides_model_claim_that_isa_was_notified(self):
        tools = V2ToolAdapters(handoff=self._adapter())
        model = ScriptedModel([
            model_tool("handoff_to_isa", {
                "operation": "create",
                "reason": "cancel_order",
                "customer_reason": "cancelar pedido",
                "order_number": "6344",
                "summary": "Quiere cancelar el pedido #6344.",
            }),
            {"content": "Listo, ya le avisé a Isa."},
        ])
        result = FredV2Agent(model_call=model, tools=tools).answer(
            "quiero cancelar el pedido #6344",
        )
        self.assertIn(ISA_PUBLIC_DISPLAY, result["reply"])
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertNotIn("ya le avisé", result["reply"])

    def test_pending_handoff_does_not_block_a_different_topic(self):
        model = ScriptedModel([{"content": "El showroom atiende con coordinación previa."}])
        result = FredV2Agent(model_call=model, tools=V2ToolAdapters()).answer(
            "¿dónde queda el showroom?",
            active_handoffs=[{
                "handoff_id": 31, "reason": "cancel_order", "order_number": "6344",
                "status": "pending", "wa_url": "https://wa.me/5491124528750?text=x",
            }],
        )
        self.assertEqual([], result["tool_calls"])
        self.assertIn("showroom", result["reply"])
        self.assertIn("ACTIVE TOPIC-SCOPED HANDOFFS", model.seen[0][-3]["content"])

    def test_same_topic_repeat_never_claims_visibility_into_isa_chat(self):
        pending = [{
            "id": 31, "reason": "cancel_order", "reason_label": "cancelar pedido",
            "order_number": "6344", "context_summary": "Quiere cancelar el pedido #6344.",
        }]
        tools = V2ToolAdapters(handoff=self._adapter(pending=pending))
        result = FredV2Agent(model_call=ScriptedModel([
            model_tool("handoff_to_isa", {
                "operation": "repeat", "handoff_id": 31,
                "reason": "cancel_order", "summary": "Pregunta por la cancelación.",
            }),
            {"content": "La cancelación ya está procesada."},
        ]), tools=tools).answer("¿qué pasó con mi cancelación?")
        self.assertIn("No puedo ver si ya hablaste con Isa", result["reply"])
        self.assertNotIn("ya está procesada", result["reply"])

    def test_customer_confirmation_prepares_only_selected_handoff(self):
        pending = [{
            "id": 31, "reason": "cancel_order", "reason_label": "cancelar pedido",
            "order_number": "6344", "context_summary": "Cancelar pedido.",
        }]
        tools = V2ToolAdapters(handoff=self._adapter(pending=pending))
        result = FredV2Agent(model_call=ScriptedModel([
            model_tool("handoff_to_isa", {
                "operation": "resolve", "handoff_id": 31,
                "reason": "cancel_order", "summary": "Ya habló con Isa.",
            }),
            {"content": "Perfecto."},
        ]), tools=tools).answer("sí, ya hablé con Isa")
        self.assertEqual("handoff_resolved", result["decision"]["action"])
        self.assertIn("Gracias por avisarme", result["reply"])
        tool_result = result["tool_results"][0]["result"]
        self.assertEqual(31, tool_result["handoff_id"])
        self.assertTrue(tool_result["resolution_prepared"])
        self.assertFalse(tool_result["resolution_committed"])
        self.assertFalse(tool_result["side_effect_executed"])

    def test_evidence_required_create_rejects_repeat_or_resolve(self):
        tools = V2ToolAdapters(
            product_lookup=lambda query: {
                "found": False, "status": "not_found", "products": [],
            },
            handoff=lambda payload: self.fail("invalid operation must not reach adapter"),
        )
        agent = FredV2Agent(model_call=ScriptedModel([
            model_tool("get_product", {"query": "Modelo inexistente"}),
            model_tool("handoff_to_isa", {
                "operation": "repeat", "handoff_id": 31,
                "reason": "custom_order",
            }),
        ]), tools=tools)
        with self.assertRaises(V2AgentExecutionError) as raised:
            agent.answer("¿tienen Modelo inexistente?")
        self.assertEqual("custom_order", raised.exception.fallback_reason)
        self.assertIn("tool_error:handoff_to_isa:ValueError", raised.exception.error_code)


class V2RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.isa_env = patch.dict(os.environ, {"ISA_WHATSAPP_NUMBER": "5491124528750"})
        self.isa_env.start()
        self.addCleanup(self.isa_env.stop)

    def test_runtime_builds_closed_tools_sends_stores_and_records_events(self):
        seen = {}

        class Agent:
            def __init__(self, *, tools):
                seen["tools"] = tools

            def answer(self, message, *, history, active_handoffs):
                seen.update(message=message, history=history, handoffs=active_handoffs)
                return {
                    "reply": "Respuesta v2",
                    "tool_calls": [], "tool_results": [], "model_calls": 1,
                    "latency_ms": 12, "errors": [],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                    "decision": {"action": "reply"},
                }

        sent = []
        stored = []
        lifecycle = []

        def prepare(conversation_id, correlation_id, reply, handoff_action):
            lifecycle.append("store")
            stored.append((conversation_id, reply))
            self.assertEqual({}, handoff_action)
            return {"message_id": 71, "body": reply, "status": "prepared"}

        def send(phone, reply):
            lifecycle.append("send")
            sent.append((phone, reply))
            return True

        def finish(conversation_id, message_id, correlation_id, delivered):
            lifecycle.append("finish")
            return {"found": True, "status": "sent" if delivered else "failed"}

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_events", return_value=1,
        ) as record_events:
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="5491100000000", conversation_id=7,
                source_message_id="wamid-v2", generation=2,
                message="hola", history=[], agent_factory=Agent,
                send_message=send,
                prepare_message=prepare,
                finish_message=finish,
            )

        self.assertIsInstance(seen["tools"], V2ToolAdapters)
        self.assertEqual([("5491100000000", "Respuesta v2")], sent)
        self.assertEqual([(7, "Respuesta v2")], stored)
        self.assertEqual(["store", "send", "finish"], lifecycle)
        self.assertTrue(result["delivered"])
        events = record_events.call_args.args[0]
        self.assertEqual("response", events[0]["event_type"])
        self.assertEqual("sent", events[0]["outcome"])
        self.assertEqual(1, events[0]["llm_calls"])

    def test_analytics_emits_response_tool_handoff_and_error_without_pii(self):
        result = {
            "tool_calls": [{
                "name": "handoff_to_isa",
                "arguments": {
                    "operation": "create", "reason": "cancel_order",
                    "summary": "ana@example.com +54 9 11 9999-8888",
                },
            }],
            "tool_results": [{
                "name": "handoff_to_isa",
                "result": {
                    "operation": "create", "handoff_id": 31, "reason": "cancel_order",
                },
            }],
            "model_calls": 2,
            "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            "errors": ["tool_error:handoff_to_isa:RuntimeError"],
        }
        events = v2_runtime._events_for_result(
            result=result, correlation_id="opaque", conversation_id=7,
            delivered=True, duration_ms=321,
        )
        self.assertEqual(
            {"response", "tool", "handoff", "error"},
            {event["event_type"] for event in events},
        )
        persisted = json.dumps(events)
        self.assertNotIn("ana@example.com", persisted)
        self.assertNotIn("9999-8888", persisted)
        self.assertNotIn("summary", persisted)

    def test_send_failure_keeps_failed_outbox_and_does_not_count_handoff(self):
        class Agent:
            def __init__(self, *, tools):
                pass

            def answer(self, *args, **kwargs):
                return {
                    "reply": "Abrí este link",
                    "tool_calls": [{
                        "name": "handoff_to_isa",
                        "arguments": {"operation": "create", "reason": "cancel_order"},
                    }],
                    "tool_results": [{
                        "name": "handoff_to_isa",
                        "result": {
                            "operation": "create", "handoff_id": 31,
                            "reason": "cancel_order",
                        },
                    }],
                    "model_calls": 1, "usage": {}, "errors": [],
                }

        stored = []
        finished = []
        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "mark_v2_handoff_delivery", return_value=1,
        ) as mark_delivery, patch.object(v2_runtime, "record_v2_events", return_value=3):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7, source_message_id="wamid-fail",
                generation=0, message="cancelar", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: False,
                prepare_message=lambda conversation_id, correlation_id, reply, action: (
                    stored.append((conversation_id, reply))
                    or {
                        "message_id": 72, "body": reply, "status": "prepared",
                        "handoff_operation": action["operation"],
                        "handoff_id": action["handoff_id"],
                        "handoff_reason": action["reason"],
                    }
                ),
                finish_message=lambda *args: finished.append(args) or {
                    "found": True, "status": "failed",
                },
            )
        self.assertFalse(result["delivered"])
        self.assertEqual([(7, "Abrí este link")], stored)
        self.assertEqual(1, len(finished))
        self.assertFalse(finished[0][-1])
        mark_delivery.assert_not_called()
        self.assertNotIn("handoff", {
            event["event_type"] for event in result["events"]
            if event["event_type"] != "tool"
        })
        response = next(event for event in result["events"] if event["event_type"] == "response")
        self.assertEqual("send_failed", response["outcome"])
        self.assertIn("whatsapp_send_failed", {
            event.get("error_type") for event in result["events"]
            if event["event_type"] == "error"
        })

    def test_agent_exception_uses_safe_v2_handoff_and_records_error(self):
        class BrokenAgent:
            def __init__(self, *, tools):
                pass

            def answer(self, *args, **kwargs):
                raise RuntimeError("secret provider detail")

        sent = []
        with patch.dict(os.environ, {"ISA_WHATSAPP_NUMBER": "5491124528750"}), patch.object(
            v2_runtime, "list_pending_v2_handoffs", return_value=[],
        ), patch.object(v2_runtime, "record_v2_handoff", return_value={
            "id": 41, "status": "prepared",
        }), patch.object(v2_runtime, "mark_v2_handoff_delivery", return_value=1), patch.object(
            v2_runtime, "record_v2_events", return_value=4,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7, source_message_id="wamid-error",
                generation=0, message="consulta", history=[], agent_factory=BrokenAgent,
                send_message=lambda phone, reply: sent.append(reply) or True,
                record_message=lambda *args: None,
            )
        self.assertTrue(result["delivered"])
        self.assertIn("https://wa.me/5491124528750", sent[0])
        self.assertIn("agent:RuntimeError", result["errors"])
        self.assertNotIn("secret provider detail", json.dumps(result["events"]))

    def test_failed_handoff_never_returns_control_to_free_model_copy(self):
        scripted = ScriptedModel([
            model_tool("handoff_to_isa", {
                "operation": "create", "reason": "cancel_order",
                "order_number": "6344", "summary": "Cancelar pedido #6344.",
            }),
            {"content": "Listo, ya le avisé a Isa."},
        ])

        class Agent:
            def __init__(self, *, tools):
                self.delegate = FredV2Agent(model_call=scripted, tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_handoff", side_effect=RuntimeError("db unavailable"),
        ), patch.object(v2_runtime, "record_v2_events", return_value=2):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-handoff-failure", generation=0,
                message="cancelar #6344", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
            )

        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertNotIn("ya le avisé", result["reply"])
        self.assertEqual(1, len(scripted.seen))
        self.assertIn("tool_error:handoff_to_isa:RuntimeError", result["errors"])
        self.assertIn("fallback:RuntimeError", result["errors"])
        self.assertEqual(1, result["model_calls"])
        self.assertEqual("handoff_to_isa", result["tool_calls"][0]["name"])

    def test_failed_evidence_tool_never_allows_invented_stock_or_price(self):
        scripted = ScriptedModel([
            model_tool("get_product", {"query": "Isabel I"}),
            {"content": "Sí, hay 12 unidades y sale $10.000."},
        ])

        class Agent:
            def __init__(self, *, tools):
                tools._product_lookup = lambda query: (_ for _ in ()).throw(
                    RuntimeError("catalog unavailable")
                )
                self.delegate = FredV2Agent(model_call=scripted, tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_handoff", return_value={"id": 42, "status": "prepared"},
        ), patch.object(v2_runtime, "mark_v2_handoff_delivery", return_value=1), patch.object(
            v2_runtime, "record_v2_events", return_value=3,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-catalog-failure", generation=0,
                message="stock y precio Isabel I", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
            )

        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertNotIn("12 unidades", result["reply"])
        self.assertNotIn("10.000", result["reply"])
        self.assertEqual(1, len(scripted.seen))
        self.assertEqual(1, result["model_calls"])
        self.assertEqual("get_product", result["tool_calls"][0]["name"])
        self.assertIn("tool_error:get_product:RuntimeError", result["errors"])

    def test_required_handoff_is_forced_if_model_ignores_reprompts(self):
        scripted = ScriptedModel([
            model_tool("get_product", {"query": "Modelo inexistente"}),
            {"content": "No aparece publicado."},
            {"content": "No aparece publicado."},
            {"content": "No aparece publicado."},
            {"content": "No aparece publicado."},
        ])

        class Agent:
            def __init__(self, *, tools):
                tools._product_lookup = lambda query: {
                    "found": False, "status": "not_found", "products": [],
                }
                self.delegate = FredV2Agent(model_call=scripted, tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_handoff", return_value={"id": 43, "status": "prepared"},
        ), patch.object(v2_runtime, "mark_v2_handoff_delivery", return_value=1), patch.object(
            v2_runtime, "record_v2_events", return_value=4,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-required-handoff", generation=0,
                message="¿tienen Modelo inexistente?", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
            )

        self.assertEqual(5, result["model_calls"])
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertEqual("custom_order", result["decision"]["reason"])
        self.assertEqual("custom_order", result["tool_calls"][-1]["arguments"]["reason"])
        self.assertIn("required_tool_missing:custom_order", result["errors"])

    def test_handoff_resolution_commits_only_after_current_delivery(self):
        pending = [{
            "id": 31, "reason": "cancel_order", "reason_label": "cancelar pedido",
            "order_number": "6344", "context_summary": "Cancelar pedido.",
        }]

        class Agent:
            def __init__(self, *, tools):
                self.delegate = FredV2Agent(model_call=ScriptedModel([
                    model_tool("handoff_to_isa", {
                        "operation": "resolve", "handoff_id": 31,
                        "reason": "cancel_order", "summary": "Ya habló con Isa.",
                    }),
                    {"content": "Perfecto."},
                ]), tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        guard = MagicMock(side_effect=[True, True])
        with patch.object(
            v2_runtime, "list_pending_v2_handoffs", return_value=pending,
        ), patch.object(v2_runtime, "acknowledge_v2_handoff", return_value={
            "found": True, "id": 31, "reason": "cancel_order",
            "status": "customer_acknowledged",
        }) as acknowledge, patch.object(v2_runtime, "record_v2_events", return_value=3):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-resolve", generation=0,
                message="ya hablé con Isa", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
                delivery_allowed=guard,
            )

        acknowledge.assert_called_once_with(7, 31)
        self.assertTrue(result["tool_results"][0]["result"]["resolution_committed"])
        self.assertIn("handoff_resolved", {
            event["event_type"] for event in result["events"]
        })

    def test_stale_handoff_resolution_never_mutates_state(self):
        pending = [{
            "id": 31, "reason": "cancel_order", "reason_label": "cancelar pedido",
            "order_number": "6344", "context_summary": "Cancelar pedido.",
        }]

        class Agent:
            def __init__(self, *, tools):
                self.delegate = FredV2Agent(model_call=ScriptedModel([
                    model_tool("handoff_to_isa", {
                        "operation": "resolve", "handoff_id": 31,
                        "reason": "cancel_order", "summary": "Ya habló con Isa.",
                    }),
                    {"content": "Perfecto."},
                ]), tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        send = MagicMock(return_value=True)
        with patch.object(
            v2_runtime, "list_pending_v2_handoffs", return_value=pending,
        ), patch.object(v2_runtime, "acknowledge_v2_handoff") as acknowledge, patch.object(
            v2_runtime, "record_v2_events", return_value=2,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-stale-resolve", generation=0,
                message="ya hablé", history=[], agent_factory=Agent,
                send_message=send, record_message=lambda *args: None,
                delivery_allowed=lambda: False,
            )

        send.assert_not_called()
        acknowledge.assert_not_called()
        self.assertFalse(result["tool_results"][0]["result"]["resolution_committed"])
        self.assertNotIn("handoff_resolved", {
            event["event_type"] for event in result["events"]
        })

    def test_platform_error_bypasses_agent_but_still_delivers_and_observes(self):
        factory = MagicMock(side_effect=AssertionError("agent must not run"))
        stored = []
        with patch.object(v2_runtime, "list_pending_v2_handoffs") as list_pending, patch.object(
            v2_runtime, "record_v2_events", return_value=2,
        ) as record_events:
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-platform", generation=0,
                message="hola", history=[], agent_factory=factory,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: stored.append(args),
                platform_error="conversation_history_unavailable",
            )

        factory.assert_not_called()
        list_pending.assert_not_called()
        self.assertTrue(result["delivered"])
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertEqual([(7, result["reply"])], stored)
        self.assertIn("platform:conversation_history_unavailable", result["errors"])
        self.assertEqual(
            {"response", "handoff", "error"},
            {event["event_type"] for event in record_events.call_args.args[0]},
        )

    def test_active_handoff_model_context_excludes_summary_label_and_link(self):
        context = v2_runtime._active_handoff_context([{
            "id": 31, "reason": "cancel_order", "reason_label": "UNTRUSTED",
            "order_number": "6344", "context_summary": "private context",
        }])
        self.assertEqual([{
            "handoff_id": 31, "reason": "cancel_order",
            "order_number": "6344", "status": "pending",
        }], context)
        rendered = json.dumps(context)
        self.assertNotIn("UNTRUSTED", rendered)
        self.assertNotIn("private context", rendered)
        self.assertNotIn("wa.me", rendered)

    def test_stale_turn_never_sends_or_stores(self):
        class Agent:
            def __init__(self, *, tools):
                pass

            def answer(self, *args, **kwargs):
                return {
                    "reply": "respuesta vieja", "tool_calls": [], "tool_results": [],
                    "model_calls": 1, "usage": {}, "errors": [],
                }

        send = MagicMock(return_value=True)
        store = MagicMock()
        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_events", return_value=1,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7, source_message_id="wamid-stale",
                generation=0, message="hola", history=[], agent_factory=Agent,
                send_message=send, record_message=store, delivery_allowed=lambda: False,
            )
        self.assertFalse(result["delivered"])
        send.assert_not_called()
        store.assert_not_called()
        response = next(event for event in result["events"] if event["event_type"] == "response")
        self.assertEqual("stale_suppressed", response["outcome"])

    def test_already_sent_outbox_prevents_duplicate_whatsapp(self):
        factory = MagicMock(side_effect=AssertionError("agent must not rerun"))
        send = MagicMock(return_value=True)
        prepare = MagicMock()
        finish = MagicMock()
        with patch.object(v2_runtime, "list_pending_v2_handoffs") as pending, patch.object(
            v2_runtime, "record_v2_events", return_value=0,
        ):
            finish.return_value = {
                "found": True, "status": "sent", "handoff_action_applied": True,
                "handoff_operation": "create", "handoff_id": 31,
                "handoff_reason": "cancel_order",
            }
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-already-sent", generation=0,
                message="hola", history=[], agent_factory=factory,
                send_message=send,
                load_message=lambda *args: {
                    "message_id": 73, "body": "respuesta original", "status": "sent",
                    "handoff_operation": "create", "handoff_id": 31,
                    "handoff_reason": "cancel_order",
                },
                prepare_message=prepare,
                finish_message=finish,
            )
        self.assertTrue(result["delivered"])
        self.assertEqual("respuesta original", result["reply"])
        factory.assert_not_called()
        pending.assert_not_called()
        send.assert_not_called()
        prepare.assert_not_called()
        finish.assert_called_once()
        self.assertIn("handoff", {event["event_type"] for event in result["events"]})

    def test_failed_outbox_retries_canonical_body_and_action_without_agent(self):
        factory = MagicMock(side_effect=AssertionError("agent must not rerun"))
        sent = []
        finished = []
        existing = {
            "message_id": 74, "body": "respuesta canónica", "status": "failed",
            "handoff_operation": "create", "handoff_id": 32,
            "handoff_reason": "purchase_intent",
        }
        with patch.object(v2_runtime, "list_pending_v2_handoffs") as pending, patch.object(
            v2_runtime, "get_v2_handoff_by_correlation",
        ) as orphan, patch.object(v2_runtime, "record_v2_events", return_value=2):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-failed-retry", generation=0,
                message="mensaje que no debe reevaluarse", history=[],
                agent_factory=factory,
                send_message=lambda phone, reply: sent.append(reply) or True,
                load_message=lambda *args: existing,
                prepare_message=MagicMock(),
                finish_message=lambda *args: finished.append(args) or {
                    "found": True, "status": "sent",
                    "handoff_operation": "create", "handoff_id": 32,
                    "handoff_reason": "purchase_intent",
                    "handoff_action_applied": True,
                },
            )
        self.assertEqual(["respuesta canónica"], sent)
        self.assertTrue(result["delivered"])
        self.assertEqual(1, len(finished))
        factory.assert_not_called()
        pending.assert_not_called()
        orphan.assert_not_called()

    def test_duplicate_replay_miss_never_runs_agent_or_sends(self):
        factory = MagicMock(side_effect=AssertionError("agent must not run"))
        send = MagicMock(return_value=True)
        with patch.object(
            v2_runtime, "get_v2_handoff_by_correlation", return_value=None,
        ), patch.object(v2_runtime, "list_pending_v2_handoffs") as pending, patch.object(
            v2_runtime, "record_v2_events",
        ) as events:
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-replay-miss", generation=0,
                message="hola", history=[], agent_factory=factory,
                send_message=send, load_message=lambda *args: None,
                prepare_message=MagicMock(), finish_message=MagicMock(),
                replay_only=True,
            )
        self.assertFalse(result["delivered"])
        self.assertEqual("duplicate_replay_miss", result["decision"]["action"])
        factory.assert_not_called()
        send.assert_not_called()
        pending.assert_not_called()
        events.assert_not_called()

    def test_orphan_handoff_rebuilds_outbox_without_agent(self):
        factory = MagicMock(side_effect=AssertionError("agent must not rerun"))
        prepared = []
        orphan = {
            "id": 41, "reason": "cancel_order", "order_number": "6344",
            "status": "prepared", "created_at": None,
            "delivered_at": None, "resolved_at": None,
        }
        with patch.object(v2_runtime, "get_v2_handoff_by_correlation", return_value=orphan), patch.object(
            v2_runtime, "list_pending_v2_handoffs",
        ) as pending, patch.object(v2_runtime, "record_v2_events", return_value=3):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-orphan", generation=0,
                message="cancelar #6344", history=[], agent_factory=factory,
                send_message=lambda phone, reply: True,
                load_message=lambda *args: None,
                prepare_message=lambda conversation_id, correlation_id, reply, action: (
                    prepared.append((reply, action)) or {
                        "message_id": 75, "body": reply, "status": "prepared",
                        "handoff_operation": action["operation"],
                        "handoff_id": action["handoff_id"],
                        "handoff_reason": action["reason"],
                    }
                ),
                finish_message=lambda *args: {
                    "found": True, "status": "sent",
                    "handoff_operation": "create", "handoff_id": 41,
                    "handoff_reason": "cancel_order",
                    "handoff_action_applied": True,
                },
            )
        factory.assert_not_called()
        pending.assert_not_called()
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertEqual("create", prepared[0][1]["operation"])
        self.assertEqual(41, prepared[0][1]["handoff_id"])

    def test_empty_model_reply_fails_closed_with_observable_handoff(self):
        class Agent:
            def __init__(self, *, tools):
                self.delegate = FredV2Agent(
                    model_call=ScriptedModel([{"content": ""}]), tools=tools,
                )

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_handoff", return_value={"id": 51, "status": "prepared"},
        ), patch.object(v2_runtime, "mark_v2_handoff_delivery", return_value=1), patch.object(
            v2_runtime, "record_v2_events", return_value=3,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-empty", generation=0,
                message="consulta", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
            )
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertIn("empty_model_reply", result["errors"])
        self.assertEqual(1, result["model_calls"])

    def test_provider_failure_counts_attempt_and_never_returns_free_prose(self):
        def broken_model(_messages):
            raise RuntimeError("provider unavailable")

        class Agent:
            def __init__(self, *, tools):
                self.delegate = FredV2Agent(model_call=broken_model, tools=tools)

            def answer(self, *args, **kwargs):
                return self.delegate.answer(*args, **kwargs)

        with patch.object(v2_runtime, "list_pending_v2_handoffs", return_value=[]), patch.object(
            v2_runtime, "record_v2_handoff", return_value={"id": 52, "status": "prepared"},
        ), patch.object(v2_runtime, "mark_v2_handoff_delivery", return_value=1), patch.object(
            v2_runtime, "record_v2_events", return_value=3,
        ):
            result = v2_runtime.run_v2_customer_turn(
                customer_phone="54911", conversation_id=7,
                source_message_id="wamid-provider", generation=0,
                message="consulta", history=[], agent_factory=Agent,
                send_message=lambda phone, reply: True,
                record_message=lambda *args: None,
            )
        self.assertIn("https://wa.me/5491124528750", result["reply"])
        self.assertIn("model_error:RuntimeError", result["errors"])
        self.assertEqual(1, result["model_calls"])


class V2EventStoreTests(unittest.TestCase):
    def test_handoff_retry_never_rewrites_canonical_reason_or_order(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (
            31, "cancel_order", "6344", "delivery_failed", None, None, None,
        )
        with patch.object(operations_store, "_connect", return_value=connection):
            result = operations_store.record_v2_handoff(
                correlation_id="opaque", conversation_id=7,
                reason="purchase_intent", order_number="9999",
            )
        sql = cursor.execute.call_args.args[0]
        self.assertIn("SET correlation_id = fred_v2_handoffs.correlation_id", sql)
        self.assertNotIn("SET reason", sql)
        self.assertEqual("cancel_order", result["reason"])
        self.assertEqual("6344", result["order_number"])

    def test_event_writer_ignores_unrecognised_pii_fields(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        with patch.object(operations_store, "_connect", return_value=connection):
            inserted = operations_store.record_v2_events([{
                "correlation_id": "opaque", "conversation_id": 7,
                "event_type": "response", "topic": "general", "outcome": "sent",
                "customer_phone": "5491199998888", "text": "texto privado",
                "reply": "respuesta privada", "latency_ms": 20,
            }])
        self.assertEqual(1, inserted)
        sql, params = cursor.execute.call_args.args
        rendered = "{} {}".format(sql, params)
        self.assertNotIn("customer_phone", rendered)
        self.assertNotIn("texto privado", rendered)
        self.assertNotIn("respuesta privada", rendered)
        delete_sql, delete_params = cursor.execute.call_args_list[1].args
        self.assertIn("DELETE FROM fred_v2_events", delete_sql)
        self.assertEqual((["opaque"],), delete_params)
        self.assertIn("event_type NOT IN ('handoff', 'handoff_resolved')", delete_sql)

    def test_migration_keeps_analytics_and_handoffs_backend_only(self):
        migration = os.path.join(ROOT, "docs", "sql", "012_fred_v2_cutover.sql")
        with open(migration, "r", encoding="utf-8") as handle:
            sql = handle.read()
        self.assertIn("fred_v2_handoffs ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("fred_v2_events ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("FROM PUBLIC, anon, authenticated", sql)
        self.assertIn("fred_v2_correlation_id", sql)
        self.assertIn("messages_fred_v2_correlation_uidx", sql)

    def test_tool_name_is_allowlisted_before_analytics_storage(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        with patch.object(operations_store, "_connect", return_value=connection):
            operations_store.record_v2_events([{
                "correlation_id": "opaque", "conversation_id": 7,
                "event_type": "tool", "tool_name": "ana@example.com",
            }])
        _sql, params = cursor.execute.call_args.args
        self.assertIn("unknown", params)
        self.assertNotIn("ana@example.com", params)

    def test_successful_correlation_is_terminal_on_retry(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [("opaque",)]
        with patch.object(operations_store, "_connect", return_value=connection):
            inserted = operations_store.record_v2_events([{
                "correlation_id": "opaque", "conversation_id": 7,
                "event_type": "response", "outcome": "send_failed",
            }])
        self.assertEqual(0, inserted)
        self.assertEqual(1, cursor.execute.call_count)
        self.assertIn("outcome = 'sent'", cursor.execute.call_args.args[0])

    def test_outbox_replay_upgrades_outcome_without_erasing_original_metrics(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        cursor.rowcount = 1
        with patch.object(operations_store, "_connect", return_value=connection):
            operations_store.record_v2_events([{
                "correlation_id": "opaque", "conversation_id": 7,
                "event_type": "response", "event_index": 0,
                "topic": "general", "outcome": "sent",
                "latency_ms": 0, "llm_calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "preserve_existing": True,
            }])
        self.assertEqual(2, cursor.execute.call_count)
        replay_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn("outcome = EXCLUDED.outcome", replay_sql)
        self.assertNotIn("topic = EXCLUDED.topic", replay_sql)
        self.assertNotIn("DELETE FROM fred_v2_events", replay_sql)


class V2OutboxStoreTests(unittest.TestCase):
    def test_prepare_outbox_is_idempotent_and_returns_canonical_body(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (
            71, "respuesta original", "sent", None,
            "create", 31, "cancel_order",
        )
        with patch.object(conversation_store, "_connect", return_value=connection):
            result = conversation_store.prepare_v2_bot_message(
                7, "opaque-correlation", "respuesta de retry",
            )
        sql, params = cursor.execute.call_args.args
        self.assertIn("ON CONFLICT (fred_v2_correlation_id)", sql)
        self.assertEqual("respuesta original", result["body"])
        self.assertEqual("sent", result["status"])
        self.assertIn("opaque-correlation", params)

    def test_finish_outbox_never_downgrades_sent_state(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            ("sent", None, None, None, None),
            ("sent", None),
        ]
        with patch.object(conversation_store, "_connect", return_value=connection):
            result = conversation_store.finish_v2_bot_message(
                7, 71, "opaque-correlation", False,
            )
        update_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn("WHEN fred_v2_delivery_status = 'sent' THEN 'sent'", update_sql)
        self.assertEqual("sent", result["status"])

    def test_finish_outbox_commits_create_handoff_and_delivery_atomically(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            ("prepared", None, "create", 31, "cancel_order"),
            ("pending",),
            ("sent", "delivered-at"),
        ]
        with patch.object(conversation_store, "_connect", return_value=connection):
            result = conversation_store.finish_v2_bot_message(
                7, 71, "opaque-correlation", True,
            )
        sql_calls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("FOR UPDATE", sql_calls[0])
        self.assertIn("UPDATE fred_v2_handoffs", sql_calls[1])
        self.assertIn("UPDATE messages", sql_calls[2])
        self.assertTrue(result["handoff_action_applied"])
        self.assertEqual("pending", result["handoff_action_status"])
        connection.commit.assert_called_once()

    def test_finish_outbox_commits_resolve_only_with_successful_delivery(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [
            ("prepared", None, "resolve", 31, "cancel_order"),
            ("customer_acknowledged",),
            ("sent", "delivered-at"),
        ]
        with patch.object(conversation_store, "_connect", return_value=connection):
            result = conversation_store.finish_v2_bot_message(
                7, 72, "resolve-correlation", True,
            )
        resolve_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn("status = 'customer_acknowledged'", resolve_sql)
        self.assertTrue(result["handoff_action_applied"])
        self.assertEqual("customer_acknowledged", result["handoff_action_status"])


class IncomingRequest:
    def __init__(self, text, state="ISA"):
        self.state = state
        self._body = {"entry": [{"changes": [{"value": {"messages": [{
            "from": "5491100000000", "id": "wamid-cutover", "text": {"body": text},
        }]}}]}]}

    async def json(self):
        return self._body


class V2WebhookCutoverTests(unittest.TestCase):
    def test_v2_returns_before_legacy_ownership_shadow_and_v1_answer(self):
        with patch.object(app, "BOT_RESPONSE_MODE", "v2"), patch.object(
            app, "CONVERSATION_DEBOUNCE_SECONDS", 0,
        ), patch.object(app, "load_history", return_value=[]), patch.object(
            app, "record_inbound_message", return_value=(7, "ISA", False),
        ), patch.object(app, "run_v2_customer_turn", return_value={"delivered": True}) as run_v2, patch.object(
            app, "answer",
        ) as v1_answer, patch.object(app, "_begin_v2_shadow_turn") as shadow, patch.object(
            app, "_relay_customer_message_to_isa",
        ) as relay, patch.object(app, "_simple_customer_reply") as v1_shortcut:
            response = asyncio.run(app.webhook_post(IncomingRequest("hola")))

        self.assertEqual(200, response.status_code)
        run_v2.assert_called_once()
        self.assertEqual("hola", run_v2.call_args.kwargs["message"])
        v1_answer.assert_not_called()
        v1_shortcut.assert_not_called()
        shadow.assert_not_called()
        relay.assert_not_called()

    def test_durable_claim_passes_grouped_history_and_generation_to_v2(self):
        body = {"entry": [{"changes": [{"value": {"messages": [{
            "from": "5491100000000", "id": "wamid-last", "text": {"body": "hola\nquiero saber"},
        }]}}]}]}
        claim = {
            "customer_phone": "5491100000000", "conversation_id": 9,
            "state": "ISA", "generation": 6,
            "history": [{"role": "user", "content": "mensaje previo"}],
        }
        with patch.object(app, "BOT_RESPONSE_MODE", "v2"), patch.object(
            app, "run_v2_customer_turn", return_value={"delivered": True},
        ) as run_v2, patch.object(app, "answer") as v1_answer:
            response = asyncio.run(app._process_webhook_body(body, persisted_claim=claim))
        self.assertEqual(200, response.status_code)
        self.assertEqual(6, run_v2.call_args.kwargs["generation"])
        self.assertEqual(claim["history"], run_v2.call_args.kwargs["history"])
        self.assertEqual("hola\nquiero saber", run_v2.call_args.kwargs["message"])
        v1_answer.assert_not_called()

    def test_v2_history_failure_stays_on_observable_v2_path(self):
        with patch.object(app, "BOT_RESPONSE_MODE", "v2"), patch.object(
            app, "CONVERSATION_DEBOUNCE_SECONDS", 0,
        ), patch.object(app, "load_history", side_effect=RuntimeError("db")), patch.object(
            app, "record_inbound_message", return_value=(7, "BOT", False),
        ), patch.object(
            app, "run_v2_customer_turn", return_value={"delivered": True},
        ) as run_v2, patch.object(app, "_send_service_fallback") as legacy_fallback, patch.object(
            app, "answer",
        ) as v1_answer:
            response = asyncio.run(app.webhook_post(IncomingRequest("hola")))

        self.assertEqual(200, response.status_code)
        run_v2.assert_called_once()
        self.assertEqual(
            "conversation_history_unavailable",
            run_v2.call_args.kwargs["platform_error"],
        )
        legacy_fallback.assert_not_called()
        v1_answer.assert_not_called()

    def test_v2_duplicate_enters_outbox_only_replay_and_never_v1(self):
        with patch.object(app, "BOT_RESPONSE_MODE", "v2"), patch.object(
            app, "CONVERSATION_DEBOUNCE_SECONDS", 0,
        ), patch.object(app, "load_history", return_value=[]), patch.object(
            app, "record_inbound_message", return_value=(7, "BOT", True),
        ), patch.object(
            app, "run_v2_customer_turn", return_value={"delivered": False},
        ) as replay, patch.object(app, "answer") as v1_answer:
            response = asyncio.run(app.webhook_post(IncomingRequest("hola")))

        self.assertEqual(200, response.status_code)
        replay.assert_called_once()
        self.assertTrue(replay.call_args.kwargs["replay_only"])
        v1_answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
