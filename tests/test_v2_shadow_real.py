import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "bot", ROOT / "evals"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import app  # noqa: E402
import operations_store  # noqa: E402
import tiendanube_tools  # noqa: E402
import v2_shadow  # noqa: E402
from run_fred_v2_shadow_eval import aggregate  # noqa: E402
from v2_shadow import ShadowReadOnlyTools, ShadowTurn  # noqa: E402


class ShadowToolSafetyTests(unittest.TestCase):
    def setUp(self):
        self.reads = []
        self.tools = ShadowReadOnlyTools(
            knowledge_search=lambda query: self._read(
                "knowledge", query, {"found": True, "context": "Showroom con coordinación."},
            ),
            order_lookup=lambda number: self._read("order", number, {
                "found": True, "order_number": number, "fulfillment_status": "UNPACKED",
                "shipping_type": "ship", "tracking": None, "carrier": None,
            }),
            product_lookup=lambda query: self._read("product", query, {
                "found": True, "products": [{"product_name": "Isabel I", "variants": []}],
            }),
        )

    def _read(self, source, value, result):
        self.reads.append((source, value))
        return result

    def test_all_data_tools_are_reads_and_handoff_is_simulated(self):
        self.tools.call("search_knowledge", {"query": "showroom"})
        self.tools.call("get_order", {"order_number": "6344"})
        self.tools.call("get_product", {"query": "Isabel I"})
        handoff = self.tools.call("handoff_to_isa", {
            "reason": "product_advice", "summary": "Necesita asesoramiento.",
        })
        self.assertEqual([
            ("knowledge", "showroom"), ("order", "6344"), ("product", "Isabel I"),
        ], self.reads)
        self.assertEqual("simulated_success", handoff["status"])
        self.assertTrue(handoff["would_handoff"])
        self.assertFalse(handoff["side_effect_executed"])

    def test_write_tool_and_live_handoff_injection_are_rejected(self):
        with self.assertRaises(ValueError):
            self.tools.call("create_checkout", {})
        with self.assertRaises(ValueError):
            ShadowReadOnlyTools(handoff=lambda payload: {"side_effect_executed": True})

    def test_tiendanube_read_adapters_use_get_and_never_mutation_methods(self):
        product_list = Mock(status_code=200)
        product_list.json.return_value = []
        product_list.raise_for_status.return_value = None
        product_detail = Mock(status_code=200)
        product_detail.json.return_value = {"id": 1, "published": False}
        product_detail.raise_for_status.return_value = None

        def get_response(url, **kwargs):
            return product_detail if url.endswith("/products/1") else product_list

        with patch.object(
            tiendanube_tools, "get_tiendanube_configuration",
            return_value={"store_id": "1", "access_token": "token", "user_agent": "test"},
        ), patch.object(tiendanube_tools._SESSION, "get", side_effect=get_response) as get, patch.object(
            tiendanube_tools._SESSION, "post",
        ) as post, patch.object(tiendanube_tools._SESSION, "put") as put, patch.object(
            tiendanube_tools._SESSION, "patch",
        ) as patch_method, patch.object(tiendanube_tools._SESSION, "delete") as delete:
            tiendanube_tools.get_order_status("6344")
            tiendanube_tools.search_products("Isabel")
            tiendanube_tools.get_product_availability(1)
        self.assertEqual(3, get.call_count)
        post.assert_not_called()
        put.assert_not_called()
        patch_method.assert_not_called()
        delete.assert_not_called()

    def test_shadow_path_never_calls_whatsapp_or_isa_sender(self):
        responses = iter((
            {"tool_calls": [{
                "id": "h1", "type": "function", "function": {
                    "name": "handoff_to_isa",
                    "arguments": json.dumps({"reason": "product_advice", "summary": "Ayuda."}),
                },
            }]},
            {"content": "Isa puede ayudarte."},
        ))
        with patch.object(app, "send_whatsapp_text") as whatsapp, patch.object(
            app, "_queue_for_isa",
        ) as queue:
            result = v2_shadow.propose_shadow_turn(
                "cuál me recomendás",
                agent=v2_shadow.FredV2Agent(
                    model_call=lambda messages: next(responses), tools=self.tools,
                ),
            )
        whatsapp.assert_not_called()
        queue.assert_not_called()
        self.assertFalse(result["tool_results"][0]["result"]["side_effect_executed"])


class ShadowRuntimeTests(unittest.TestCase):
    def tearDown(self):
        v2_shadow.clear_shadow_turn()

    def test_flag_defaults_false_and_captures_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FRED_V2_SHADOW_ENABLED", None)
            self.assertFalse(v2_shadow.begin_shadow_turn(
                conversation_id=7, generation=2, customer_phone="54911",
                source_message_id="wamid", message="hola", history=[],
            ))
        self.assertIsNone(v2_shadow.current_shadow_turn.get())

    def test_observer_dispatch_is_non_blocking_and_only_after_v1_delivery(self):
        with patch.dict(os.environ, {"FRED_V2_SHADOW_ENABLED": "true"}):
            self.assertTrue(v2_shadow.begin_shadow_turn(
                conversation_id=7, generation=2, customer_phone="54911",
                source_message_id="wamid", message="hola", history=[],
            ))
        fake_future = Mock()
        started = time.monotonic()
        with patch.object(v2_shadow._supervisor_executor, "submit", return_value=fake_future):
            self.assertTrue(v2_shadow.observe_v1_delivery("54911", "respuesta v1"))
        self.assertLess(time.monotonic() - started, 0.1)
        fake_future.add_done_callback.assert_called_once()
        self.assertIsNone(v2_shadow.current_shadow_turn.get())
        v2_shadow._pending_slots.release()

    def test_timeout_and_model_exception_are_recorded_fail_open(self):
        turn = ShadowTurn("cid", 7, 3, "54911", "hash", "hola", tuple())
        records = []
        with patch.object(v2_shadow, "shadow_timeout_seconds", return_value=0.01):
            v2_shadow._run_and_record(
                turn, "respuesta v1",
                proposer=lambda *args, **kwargs: (time.sleep(0.05) or {}),
                recorder=lambda row: records.append(row) or True,
            )
        self.assertEqual("shadow_timeout", records[0]["error_type"])
        self.assertFalse(records[0]["side_effects"])

    def test_log_store_rejects_side_effect_before_database_access(self):
        with patch.object(operations_store, "_connect") as connect:
            with self.assertRaises(ValueError):
                operations_store.record_v2_shadow_observation({"side_effects": True})
        connect.assert_not_called()

    def test_shadow_store_writes_only_its_observation_table(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.rowcount = 1
        with patch.object(operations_store, "_connect", return_value=connection):
            self.assertTrue(operations_store.record_v2_shadow_observation({
                "correlation_id": "cid", "conversation_id": 7, "side_effects": False,
            }))
        [sql] = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("INSERT INTO fred_v2_shadow_observations", sql)
        for forbidden in ("UPDATE conversations", "INSERT INTO messages", "fred_core_state"):
            self.assertNotIn(forbidden, sql)
        connection.commit.assert_called_once()

    def test_pii_is_redacted_from_persisted_shadow_payload(self):
        turn = ShadowTurn(
            "cid", 7, 3, "5491111111111", "hash",
            "Soy Ana, mi mail es ana@example.com y mi teléfono +54 9 11 1234-5678",
            tuple(),
        )
        observation = v2_shadow._observation(turn, "Escribinos al +54 9 11 9999-8888", {
            "proposed_reply": "Te escribimos a ana@example.com", "tools": [],
            "tool_results": [], "tokens": {},
        })
        persisted = json.dumps(observation, ensure_ascii=False)
        self.assertNotIn("ana@example.com", persisted)
        self.assertNotIn("1234-5678", persisted)
        self.assertNotIn("9999-8888", persisted)

    def test_app_helpers_are_noop_when_flag_is_false(self):
        with patch.object(app, "FRED_V2_SHADOW_ENABLED", False), patch.dict(
            sys.modules, {"v2_shadow": None}, clear=False,
        ):
            self.assertFalse(app._begin_v2_shadow_turn(unused=True))
            self.assertFalse(app._observe_v2_shadow_delivery("54911", "v1"))

    def test_sender_failure_in_shadow_observer_cannot_change_v1_success(self):
        response = Mock()
        response.status_code = 200
        response.text = "ok"
        response.raise_for_status.return_value = None
        with patch.object(app, "FRED_V2_SHADOW_ENABLED", True), patch.object(
            app.requests, "post", return_value=response,
        ), patch.object(app, "_real_outbound_is_blocked", return_value=False), patch.object(
            v2_shadow, "observe_v1_delivery", side_effect=RuntimeError("shadow broken"),
        ):
            self.assertTrue(app.send_whatsapp_text("54911", "respuesta v1"))

    def test_real_pipeline_arms_shadow_then_dispatches_after_v1_send(self):
        body = {"entry": [{"changes": [{"value": {"messages": [{
            "from": "54911", "id": "wamid-shadow", "text": {"body": "hola"},
        }]}}]}]}
        response = Mock()
        response.status_code = 200
        response.text = "ok"
        response.raise_for_status.return_value = None
        with patch.object(app, "FRED_V2_SHADOW_ENABLED", True), patch.object(
            app, "BOT_RESPONSE_MODE", "agent",
        ), patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0), patch.object(
            app, "record_inbound_message", return_value=(7, "BOT", False),
        ), patch.object(app, "load_history", return_value=[]), patch.object(
            app, "get_fred_core_state", return_value={"mode": "CHAT"},
        ), patch.object(app, "get_active_sales_intake", return_value=None), patch.object(
            app, "record_bot_message",
        ), patch.object(app.requests, "post", return_value=response), patch.object(
            app, "_real_outbound_is_blocked", return_value=False,
        ), patch.object(app, "_begin_v2_shadow_turn", return_value=True) as begin, patch.object(
            app, "_observe_v2_shadow_delivery", return_value=True,
        ) as observe:
            result = asyncio.run(app._process_webhook_body(body))
        self.assertEqual(200, result.status_code)
        begin.assert_called_once()
        observe.assert_called_once_with("54911", "¡Hola! 😊 ¿En qué te puedo ayudar?")


class ShadowEvaluationTests(unittest.TestCase):
    def test_aggregator_reports_requested_metrics_and_blockers(self):
        report = aggregate([{
            "v1_outcome": "REVIEW", "rubric_outcome": "PASS",
            "v2_response_redacted": "Tu pedido está empaquetado.",
            "v2_tool_calls": [{"name": "get_order", "arguments": {"order_number": "6344"}}],
            "v2_tool_results": [{"name": "get_order", "result": {"order_number": "6344"}}],
            "v2_llm_calls": 2, "v2_prompt_tokens": 100, "v2_completion_tokens": 20,
            "v2_latency_ms": 1500, "v2_handoff_reason": "", "error_type": "",
            "side_effects": False, "expected_order_number": "6344",
        }])
        self.assertEqual(1, report["total"])
        self.assertEqual(1, report["pass"])
        self.assertEqual(1.0, report["order_accuracy"])
        self.assertEqual(1.0, report["win_rate"]["v2"])
        self.assertIn("latency_p95_ms", report)
        self.assertIn("stale_context_failures", report)


if __name__ == "__main__":
    unittest.main()
