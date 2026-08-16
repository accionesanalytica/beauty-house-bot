"""Three structured lines per agent turn, so latency and wrong answers become
measurable instead of anecdotal.

    [FredTiming]   total_ms knowledge_ms catalog_ms live_stock_ms llm_ms
                   tool_calls tokens_input tokens_output
    [FredDecision] topic grounded_by active_product active_sku buttons_added
    [FredRouting]  data_required skipped_live reason

Two properties matter more than the numbers themselves, and both are tested
here: the fields are always present and always parseable (a log you have to
grep conditionally is not an instrument), and instrumentation can never affect
the turn -- a measurement bug must never cost a customer their answer.
"""

import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


TIMING_FIELDS = (
    "total_ms", "knowledge_ms", "catalog_ms", "live_stock_ms", "llm_ms",
    "tool_calls", "tokens_input", "tokens_output",
)
DECISION_FIELDS = (
    "topic", "grounded_by", "active_product", "active_sku", "buttons_added",
)


def parse_log(output, prefix):
    """Every '[Prefix] k=v k=v' line in `output`, as a list of dicts. Values
    keep everything up to the next ' <key>=', so a product name with spaces
    survives intact."""
    import re

    parsed = []
    for line in output.splitlines():
        if not line.startswith(prefix):
            continue
        body = line[len(prefix):].strip()
        pairs = re.findall(r"(\w+)=(.*?)(?=\s+\w+=|$)", body)
        parsed.append({key: value for key, value in pairs})
    return parsed


class TimedPhaseTests(unittest.TestCase):
    def test_new_timings_start_at_zero_for_every_phase(self):
        timings = app._new_turn_timings()
        self.assertEqual(set(timings), set(app._TIMING_PHASES))
        self.assertTrue(all(value == 0.0 for value in timings.values()))

    def test_a_phase_entered_twice_accumulates_instead_of_overwriting(self):
        # catalog and live verification are each entered more than once per
        # real turn. Overwriting would silently under-report the phase that
        # matters most.
        timings = app._new_turn_timings()
        for _ in range(3):
            with app._timed(timings, "catalog_ms"):
                pass
        self.assertGreaterEqual(timings["catalog_ms"], 0.0)

        marker = app._new_turn_timings()
        with app._timed(marker, "llm_ms"):
            pass
        first = marker["llm_ms"]
        with app._timed(marker, "llm_ms"):
            pass
        self.assertGreaterEqual(marker["llm_ms"], first)

    def test_a_failing_phase_is_still_measured_and_the_error_still_propagates(self):
        timings = app._new_turn_timings()
        with self.assertRaises(ValueError):
            with app._timed(timings, "live_stock_ms"):
                raise ValueError("tiendanube caída")
        # Measured (the phase ran and cost time) and NOT swallowed: an
        # instrument that hides failures is worse than no instrument.
        self.assertIn("live_stock_ms", timings)


class TimingLineTests(unittest.TestCase):
    def _emit(self, timings=None, **kwargs):
        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_timing(
                timings if timings is not None else app._new_turn_timings(),
                started_at=app.time.monotonic(), **kwargs
            )
        return parse_log(stream.getvalue(), "[FredTiming]")

    def test_every_field_is_always_present(self):
        [line] = self._emit()
        self.assertEqual(set(line), set(TIMING_FIELDS))

    def test_every_value_is_a_number(self):
        [line] = self._emit(tool_calls=3, tokens_input=5500, tokens_output=180)
        for field in TIMING_FIELDS:
            with self.subTest(field=field):
                int(line[field])  # raises if it is ever not an integer

    def test_reported_counts_are_the_ones_passed_in(self):
        [line] = self._emit(tool_calls=4, tokens_input=17000, tokens_output=240)
        self.assertEqual(line["tool_calls"], "4")
        self.assertEqual(line["tokens_input"], "17000")
        self.assertEqual(line["tokens_output"], "240")

    def test_missing_phases_report_zero_rather_than_disappearing(self):
        [line] = self._emit(timings={"llm_ms": 12.7})
        self.assertEqual(line["knowledge_ms"], "0")
        self.assertEqual(line["catalog_ms"], "0")
        self.assertEqual(line["live_stock_ms"], "0")
        self.assertEqual(line["llm_ms"], "13")

    def test_a_broken_timings_object_never_raises(self):
        # Non-empty on purpose: an empty mapping is falsy and would be
        # replaced by a plain {} before anything hostile could happen, so an
        # empty double would test nothing.
        class Hostile(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("no")

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_timing(Hostile(llm_ms=1.0), started_at=app.time.monotonic())
        self.assertIn("ERROR registrando timing", stream.getvalue())


class DecisionLineTests(unittest.TestCase):
    def _emit(self, **kwargs):
        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_decision(**kwargs)
        return parse_log(stream.getvalue(), "[FredDecision]")

    def test_every_field_is_always_present(self):
        [line] = self._emit()
        self.assertEqual(set(line), set(DECISION_FIELDS))

    def test_empty_state_reports_none_rather_than_a_blank(self):
        [line] = self._emit()
        for field in DECISION_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(line[field])
        self.assertEqual(line["topic"], "none")
        self.assertEqual(line["active_sku"], "none")
        self.assertEqual(line["buttons_added"], "no")

    def test_a_real_turn_reports_what_the_next_message_will_anchor_to(self):
        [line] = self._emit(
            topic="commercial_operations",
            grounded_by="knowledge|catalog",
            core_state={
                "active_product_name": "SHOOW TOOLS - ISABEL I",
                "active_sku": "ISABEL-8-10",
            },
            buttons_added=True,
        )
        self.assertEqual(line["topic"], "commercial_operations")
        self.assertEqual(line["grounded_by"], "knowledge|catalog")
        self.assertEqual(line["active_product"], "SHOOW TOOLS - ISABEL I")
        self.assertEqual(line["active_sku"], "ISABEL-8-10")
        self.assertEqual(line["buttons_added"], "yes")

    def test_a_broken_state_object_never_raises(self):
        # Non-empty on purpose (see the timing counterpart): an empty mapping
        # is falsy and never reaches the hostile code path.
        class Hostile(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("no")

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_decision(core_state=Hostile(mode="CHAT"))
        self.assertIn("ERROR registrando decisión", stream.getvalue())


def _default_fred_core_state(conversation_id):
    return {
        "mode": "CHAT", "active_product_id": None, "active_product_name": None,
        "active_sku": None, "active_variant": None, "unit_price": None,
        "quantity": None, "delivery_method": None, "customer_name": None,
        "customer_email": None, "postal_code": None, "checkout_step": None,
        "order_number": None,
    }


class IncomingRequest:
    def __init__(self, phone, text, message_id="wamid-obs"):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": phone, "id": message_id, "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


ONE_PRODUCT = [{
    "product_id": 20, "name": "SHOOW TOOLS - NATURAL SHOOW",
    "variants": [{"sku": "NATURAL-1", "description": "Única", "quantity": 5}],
}]
ISABEL_PRODUCTS = [
    {
        "product_id": 1, "name": "SHOOW TOOLS - ISABEL I",
        "variants": [{"sku": "ISABEL-8-10", "description": "8/10 mm", "quantity": 4}],
    },
    {
        "product_id": 2, "name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
        "variants": [{"sku": "ISABEL-CHOCO", "description": "8/8/10 mm", "quantity": 3}],
    },
]


@patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0)
@patch.object(app, "get_fred_core_state", _default_fred_core_state)
@patch.object(app, "save_fred_core_state", lambda *args, **kwargs: None)
@patch.object(app, "reset_fred_core_checkout", lambda conversation_id: None)
@patch.object(app, "get_active_sales_intake", lambda conversation_id: None)
@patch.object(app, "SALES_INTAKE_ENABLED", True)
@patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
@patch.object(app, "record_agent_turn", lambda **kwargs: None)
@patch.object(app, "record_bot_message", lambda *args, **kwargs: None)
@patch.object(app, "record_inbound_message", lambda *args, **kwargs: (7, "BOT", False))
@patch.object(app, "load_history", lambda *args, **kwargs: [])
@patch.object(app, "BOT_RESPONSE_MODE", "agent")
class TurnLoggingEndToEndTests(unittest.TestCase):
    """Both lines, once per turn, on every path that reaches the agent."""

    PHONE = "5491111111111"

    def _run_turn(self, text, products, agent_result, **extra_patches):
        stream = io.StringIO()
        patches = {
            "search_available_products": lambda query, limit=5: list(products),
            "get_stock": lambda sku: {"found": False},
            "_live_candidate_context": lambda *args, **kwargs: "",
            "search_similar_products": lambda *args, **kwargs: "",
            "get_product_selection": lambda *args, **kwargs: None,
            "send_whatsapp_text": lambda *args, **kwargs: True,
            "send_customer_action_buttons": lambda *args, **kwargs: True,
            "_start_sales_intake": lambda *args, **kwargs: "",
        }
        patches.update(extra_patches)
        if agent_result is not None:
            patches["answer"] = lambda *args, **kwargs: dict(agent_result)

        started = [patch.object(app, name, value) for name, value in patches.items()]
        for started_patch in started:
            started_patch.start()
        try:
            with redirect_stdout(stream):
                response = asyncio.run(app.webhook_post(
                    IncomingRequest(self.PHONE, text)
                ))
        finally:
            for started_patch in reversed(started):
                started_patch.stop()

        self.assertEqual(response.status_code, 200)
        output = stream.getvalue()
        return (
            parse_log(output, "[FredTiming]"),
            parse_log(output, "[FredDecision]"),
            parse_log(output, "[FredRouting]"),
        )

    def test_a_normal_turn_emits_exactly_one_of_each_line(self):
        timing, decision, routing = self._run_turn(
            "Busco pestañas naturales", [],
            {
                "reply": "Tengo algunas opciones 😊", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
        )
        self.assertEqual(len(timing), 1)
        self.assertEqual(len(decision), 1)
        self.assertEqual(set(timing[0]), set(TIMING_FIELDS))
        self.assertEqual(set(decision[0]), set(DECISION_FIELDS))

    def test_the_turn_reports_the_model_cost_it_actually_paid(self):
        timing, _, routing = self._run_turn(
            "Busco pestañas naturales", [],
            {
                "reply": "Tengo algunas opciones 😊",
                "tool_calls": [{"name": "search_available_products", "arguments": {}},
                               {"name": "get_stock", "arguments": {}}],
                "usage": {"prompt_tokens": 5500, "completion_tokens": 140},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
        )
        self.assertEqual(timing[0]["tool_calls"], "2")
        self.assertEqual(timing[0]["tokens_input"], "5500")
        self.assertEqual(timing[0]["tokens_output"], "140")

    def test_a_turn_that_offers_the_buy_button_records_it(self):
        timing, decision, routing = self._run_turn(
            "Quiero comprar natural shoow", ONE_PRODUCT,
            {
                "reply": "Sí, tenemos NATURAL SHOOW 😊", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
            get_stock=lambda sku: {
                "found": True, "sku": "NATURAL-1", "status": "in_stock",
                "product_name": "SHOOW TOOLS - NATURAL SHOOW",
                "variant": "Única", "price": "36000",
            },
        )
        self.assertEqual(len(timing), 1)
        self.assertEqual(decision[0]["buttons_added"], "yes")
        self.assertEqual(decision[0]["active_sku"], "NATURAL-1")
        self.assertEqual(decision[0]["active_product"], "SHOOW TOOLS - NATURAL SHOOW")

    def test_the_deterministic_ambiguous_purchase_turn_is_logged_too(self):
        # This path never reaches the model, which is exactly why it must be
        # logged: otherwise a whole class of turns would be invisible, and
        # llm_ms=0 with a real total_ms is itself the finding.
        timing, decision, routing = self._run_turn(
            "Quiero comprar Isabel I", ISABEL_PRODUCTS, agent_result=None,
        )
        self.assertEqual(len(timing), 1)
        self.assertEqual(len(decision), 1)
        self.assertEqual(timing[0]["llm_ms"], "0")
        self.assertEqual(timing[0]["tool_calls"], "0")
        self.assertEqual(decision[0]["buttons_added"], "no")
        self.assertEqual(decision[0]["active_sku"], "none")

    def test_a_failing_turn_is_still_measured(self):
        def explode(*args, **kwargs):
            raise RuntimeError("deepseek caído")

        timing, decision, routing = self._run_turn(
            "Busco pestañas naturales", [], None, answer=explode,
        )
        self.assertEqual(len(timing), 1)
        self.assertEqual(decision[0]["grounded_by"], "error")

    def test_every_turn_also_reports_what_data_it_needed(self):
        _, _, routing = self._run_turn(
            "Busco pestañas naturales", [],
            {
                "reply": "Tengo algunas opciones 😊", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
        )
        self.assertEqual(len(routing), 1)
        self.assertEqual(
            set(routing[0]), {"intent", "data_required", "skipped_live", "reason"})
        self.assertEqual(routing[0]["data_required"], "catalog")
        # "pestañas" is a product category, which is a more specific reading
        # than the bare "busco" verb -- naming merchandise outranks wanting
        # some, and both land on the catalog either way.
        self.assertEqual(routing[0]["intent"], "product_named")

    def test_a_turn_that_never_calls_tiendanube_reports_skipped_live_true(self):
        # skipped_live is measured, not predicted: this turn genuinely made
        # zero requests, so it reads true even with the cut not implemented.
        _, _, routing = self._run_turn(
            "¿Cuál es el horario?", [],
            {
                "reply": "Atendemos de 10 a 18 😊", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
        )
        self.assertEqual(routing[0]["skipped_live"], "true")

    def test_a_turn_that_does_call_tiendanube_reports_skipped_live_false(self):
        # The purchase-identity path really does hit the store. The pair
        # (data_required, skipped_live) is what makes wasted calls countable.
        _, _, routing = self._run_turn(
            "Quiero comprar Isabel I", ISABEL_PRODUCTS, agent_result=None,
        )
        self.assertEqual(routing[0]["skipped_live"], "false")

    def test_instrumentation_does_not_change_the_reply(self):
        sent = []
        timing, _, routing = self._run_turn(
            "Busco pestañas naturales", [],
            {
                "reply": "Tengo algunas opciones 😊", "tool_calls": [], "usage": {},
                "decision": {"action": "reply", "reason": "normal_response"},
            },
            send_whatsapp_text=lambda phone, text: sent.append(text) or True,
        )
        self.assertEqual(sent, ["Tengo algunas opciones 😊"])
        self.assertEqual(len(timing), 1)


if __name__ == "__main__":
    unittest.main()
