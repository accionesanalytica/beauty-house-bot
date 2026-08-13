"""Tests for the proactive set_turn_decision nudge in agent.answer().

Root cause it targets (see problem_a_trace.json audit): a discovery turn with
no clean catalog match can make the model keep re-searching every round
without ever calling set_turn_decision. The existing reactive nudge only
fires once the model pauses and returns empty tool_calls; if it never
pauses, MAX_TOOL_ROUNDS/MAX_TOOL_CALLS_PER_TURN run out first and any late
set_turn_decision call is silently dropped by the tool-budget guard before
validate_model_decision ever sees it. These tests exercise the proactive
nudge added to close that gap, entirely offline (DeepSeek and tools mocked).
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import agent  # noqa: E402
from decision_schema import validate_model_decision  # noqa: E402

NUDGE_MARKER = "No seguir buscando indefinidamente"


def search_call(call_id, query):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "search_products",
            "arguments": json.dumps({"query": query}),
        },
    }


def get_stock_call(call_id, sku):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "get_stock",
            "arguments": json.dumps({"sku": sku}),
        },
    }


def decision_call(payload, call_id="decision-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "set_turn_decision",
            "arguments": json.dumps(payload, ensure_ascii=False),
        },
    }


def product_decision(match_type, requested, matched="", requested_type=None, matched_type=None):
    return {
        "action": "reply",
        "reason": "normal_response",
        "summary": "Cerró la decisión de discovery con la evidencia disponible.",
        "response_mode": "product_discovery",
        "match_type": match_type,
        "requested_product": requested,
        "matched_product": matched,
        "requested_product_type": requested_type or requested,
        "matched_product_type": "" if match_type == "no_match" else (matched_type or matched),
        "required_checks": [],
    }


def tracking_side_effect(responses):
    """agent.py mutates `messages` in place across the whole turn, so
    Mock.call_args_list ends up with every entry pointing at the SAME final
    list object — useless for telling "was the nudge present at call N"
    after the fact. Snapshot presence at the moment of each call instead."""
    presence_log = []

    def _side_effect(messages):
        presence_log.append(any(
            NUDGE_MARKER in str(message.get("content", ""))
            for message in messages if message.get("role") == "system"
        ))
        return responses[len(presence_log) - 1]

    return _side_effect, presence_log


class DiscoveryNudgeTests(unittest.TestCase):
    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_discovery_burning_many_searches_gets_nudged_and_closes(self, ask_model, run_tool):
        # Rounds 0 and 1 keep searching without ever proposing a decision —
        # exactly the "dramático" pattern from the audit. By round_number==2
        # the proactive nudge must fire before the 3rd call to the model.
        decision = product_decision("no_match", "pestañas dramáticas")
        responses = [
            {"content": "", "tool_calls": [search_call("s1", "pestañas dramáticas")], "_fred_usage": {}},
            {"content": "", "tool_calls": [search_call("s2", "pestañas cat eye")], "_fred_usage": {}},
            {"content": "", "tool_calls": [decision_call(decision)], "_fred_usage": {}},
            # After set_turn_decision succeeds, the loop still needs one more
            # round with empty tool_calls to actually render and return.
            {"content": "texto ignorado; product_discovery renderiza determinísticamente", "_fred_usage": {}},
        ]
        ask_model.side_effect, presence_log = tracking_side_effect(responses)
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)}
            if name == "set_turn_decision" else []
        )

        result = agent.answer("Quiero pestañas, algo más dramático", history=[])

        self.assertEqual(ask_model.call_count, 4)
        # Nudge must be absent for calls 0 and 1 (rounds 0 and 1), and present
        # from call 2 onward (round_number == 2), matching the threshold.
        self.assertEqual(presence_log, [False, False, True, True])
        self.assertEqual(result["decision"]["response_mode"], "product_discovery")
        self.assertEqual(result["decision"]["match_type"], "no_match")
        self.assertNotEqual(result["decision"]["action"], "clarify_product")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_quick_lookup_never_reaches_the_nudge_threshold(self, ask_model, run_tool):
        # A normal lookup resolves within 1-2 rounds (well under round>=2 or
        # 6 tool calls) in every case measured for D1's benchmark. It must
        # never see the nudge simply because it stays under that budget.
        stock = {
            "found": True, "sku": "TAYLOR-1", "product_name": "Taylor",
            "variant": "default", "status": "in_stock", "quantity": 5, "price": "28000",
        }
        decision = product_decision(
            "exact_match", "Taylor", "Taylor",
            requested_type="pestañas", matched_type="pestañas",
        )
        responses = [
            {
                "content": "", "_fred_usage": {},
                "tool_calls": [get_stock_call("g1", "TAYLOR-1"), decision_call(decision)],
            },
            {"content": "Sale $28.000 y tenemos stock.", "_fred_usage": {}},
        ]
        ask_model.side_effect, presence_log = tracking_side_effect(responses)
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)} if name == "set_turn_decision"
            else stock if name == "get_stock" else []
        )

        agent.answer("¿Cuánto salen las Taylor?", history=[])

        self.assertEqual(ask_model.call_count, 2)
        self.assertEqual(presence_log, [False, False])

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_real_ambiguity_resolved_immediately_is_unaffected(self, ask_model, run_tool):
        # A genuine no_match resolved on the very first attempt (as seen in
        # ambiguity-vague-model-01 / ambiguity-nonexistent-01 in the D1
        # benchmark) must behave exactly as before: no nudge, same reply.
        decision = product_decision("no_match", "Valentina Deluxe")
        responses = [
            {"content": "", "tool_calls": [decision_call(decision)], "_fred_usage": {}},
            # One more round with empty tool_calls is required to render+return.
            {"content": "texto ignorado; no_match renderiza determinísticamente", "_fred_usage": {}},
        ]
        ask_model.side_effect, presence_log = tracking_side_effect(responses)
        run_tool.side_effect = lambda name, arguments: (
            {"decision_recorded": validate_model_decision(arguments)}
            if name == "set_turn_decision" else []
        )

        result = agent.answer("¿Tenés las Valentina Deluxe?", history=[])

        self.assertEqual(ask_model.call_count, 2)
        self.assertEqual(presence_log, [False, False])
        self.assertIn("No encontré", result["reply"])
        self.assertEqual(result["decision"]["match_type"], "no_match")

    @patch.object(agent, "_run_tool")
    @patch.object(agent, "_ask_deepseek")
    def test_existing_decision_is_never_nudged_twice(self, ask_model, run_tool):
        # A decision proposed early (round 0) must block the proactive nudge
        # even if the model keeps calling tools well past the threshold
        # (e.g. re-verifying stock) — the "not proposed_decision" guard.
        decision = product_decision(
            "exact_match", "pestañas naturales", "Foxy #1",
            requested_type="pestañas", matched_type="pestañas",
        )
        responses = [
            {"content": "", "tool_calls": [decision_call(decision), search_call("s0", "foxy 1")], "_fred_usage": {}},
            {"content": "", "tool_calls": [get_stock_call("g1", "FOXY-1")], "_fred_usage": {}},
            {"content": "", "tool_calls": [get_stock_call("g2", "FOXY-1")], "_fred_usage": {}},
            {"content": "Listo, ya está confirmado.", "_fred_usage": {}},
        ]
        ask_model.side_effect, presence_log = tracking_side_effect(responses)
        stock = {
            "found": True, "sku": "FOXY-1", "product_name": "Foxy #1",
            "variant": "default", "status": "in_stock", "quantity": 2, "price": "25000",
        }

        def run_tool_side_effect(name, arguments):
            if name == "set_turn_decision":
                return {"decision_recorded": validate_model_decision(arguments)}
            if name == "get_stock":
                return stock
            return []

        run_tool.side_effect = run_tool_side_effect

        result = agent.answer("Quiero pestañas, algo natural", history=[])

        self.assertEqual(ask_model.call_count, 4)  # reached round_number >= 2
        self.assertEqual(presence_log, [False, False, False, False],
                          "no debía re-nudgear con una decisión ya propuesta")
        self.assertEqual(result["decision"]["match_type"], "exact_match")


if __name__ == "__main__":
    unittest.main()
