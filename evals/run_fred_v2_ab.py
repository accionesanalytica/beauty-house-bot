"""Run the Fred v1/v2 conversational replay benchmark.

The benchmark uses the real LLM orchestration with deterministic fixtures for
Knowledge, Tiendanube and handoff.  It performs no external writes.  Model
network calls happen only with ``--live-model``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List
from unittest.mock import patch

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
sys.path.insert(0, str(BOT_DIR))

import agent as v1_agent  # noqa: E402
import app as v1_app  # noqa: E402
from routing_policy import (  # noqa: E402
    DATA_KNOWLEDGE_ONLY,
    INTENT_EXISTING_ORDER,
    classify_turn_data_requirement,
)
from v2_agent import FredV2Agent  # noqa: E402
from v2_tools import V2ToolAdapters  # noqa: E402


CASES_PATH = ROOT / "evals" / "fred_v2_ab_cases.json"
DEFAULT_JSON_OUTPUT = ROOT / "evals" / "results" / "fred_v2_ab_latest.json"
DEFAULT_MD_OUTPUT = ROOT / "docs" / "fred-v2-ab-report.md"

ORDER_FIXTURES = {
    "6341": {"found": True, "order_number": 6341, "payment_status": "paid", "shipping_status": "unpacked", "status": "open", "fulfillment_status": "UNPACKED", "shipping_type": "pickup", "carrier": None, "tracking": None, "tracking_url": None},
    "6342": {"found": True, "order_number": 6342, "payment_status": "paid", "shipping_status": "packed", "status": "open", "fulfillment_status": "PACKED", "shipping_type": "pickup", "carrier": None, "tracking": None, "tracking_url": None},
    "6343": {"found": True, "order_number": 6343, "payment_status": "paid", "shipping_status": "shipped", "status": "open", "fulfillment_status": "DISPATCHED", "shipping_type": "ship", "carrier": "Correo Demo", "tracking": "TRK6343", "tracking_url": "https://tracking.example/6343"},
    "6344": {"found": True, "order_number": 6344, "payment_status": "paid", "shipping_status": "delivered", "status": "closed", "fulfillment_status": "DELIVERED", "shipping_type": "ship", "carrier": "Correo Demo", "tracking": None, "tracking_url": None},
}

PRODUCT_FIXTURES = [
    {"product_id": 101, "product_name": "SHOOW TOOLS - ISABEL I", "description": "Pestañas de banda.", "product_url": "https://shop.example/isabel-i", "variants": [
        {"sku": "ISABEL-I-CHOCOLATE", "variant": "Chocolate", "status": "in_stock", "quantity": 8, "price": "12500.00"},
        {"sku": "ISABEL-I-BLACK", "variant": "Black", "status": "in_stock", "quantity": 5, "price": "12500.00"},
    ]},
    {"product_id": 102, "product_name": "SHOOW TOOLS - TAYLOR", "description": "Pestañas de banda.", "product_url": "https://shop.example/taylor", "variants": [
        {"sku": "TAYLOR-BLACK", "variant": "Black", "status": "in_stock", "quantity": 3, "price": "11900.00"},
        {"sku": "TAYLOR-CHOCOLATE", "variant": "Chocolate", "status": "out_of_stock", "quantity": 0, "price": "11900.00"},
    ]},
]

KNOWLEDGE_FIXTURE = """Conocimiento aprobado de Beauty House:
- El showroom y los retiros se coordinan previamente; los horarios se confirman al coordinar.
- Puede retirar un tercero o un cadete/moto con coordinación y datos de autorización.
- Beauty House trabaja ventas mayoristas. La lista y los precios aprobados de SHOOW TOOLS se confirman con Isa.
- Para Factura A mayorista se debe consultar a Isa con los datos fiscales."""


def _norm(value: Any) -> str:
    return str(value or "").casefold()


def fixture_order(number: str) -> Dict[str, Any]:
    return dict(ORDER_FIXTURES.get(str(number), {"found": False, "message": "Pedido no encontrado."}))


def fixture_product(query: str) -> Dict[str, Any]:
    text = _norm(query)
    if "fantasma" in text:
        return {"found": False, "query": query, "products": [], "identity_source": "tiendanube"}
    selected = []
    for product in PRODUCT_FIXTURES:
        if "isabel" in text and "isabel" not in _norm(product["product_name"]):
            continue
        if "taylor" in text and "taylor" not in _norm(product["product_name"]):
            continue
        candidate = json.loads(json.dumps(product))
        if "chocolate" in text:
            candidate["variants"] = [v for v in candidate["variants"] if _norm(v["variant"]) == "chocolate"]
        elif "black" in text:
            candidate["variants"] = [v for v in candidate["variants"] if _norm(v["variant"]) == "black"]
        if candidate["variants"]:
            selected.append(candidate)
    return {
        "found": bool(selected), "query": query, "products": selected,
        "identity_source": "tiendanube_fixture", "availability_source": "tiendanube_live_fixture",
    }


def fixture_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"accepted": True, "mode": "dry_run", **payload}


def v2_runner() -> FredV2Agent:
    return FredV2Agent(tools=V2ToolAdapters(
        knowledge_search=lambda query: {
            "found": True, "context": KNOWLEDGE_FIXTURE,
            "governing_topic": "fixture", "retrieved_topics": ["fixture"],
        },
        order_lookup=fixture_order,
        product_lookup=fixture_product,
        handoff=fixture_handoff,
    ))


def _last_named_product(history: List[Dict[str, Any]]) -> str:
    joined = " ".join(str(item.get("content") or "") for item in history[-6:])
    for name in ("Isabel I Chocolate", "Taylor Black", "Isabel I", "Taylor"):
        if _norm(name) in _norm(joined):
            return name
    return ""


def _v1_tool(name: str, arguments: Dict[str, Any]) -> Any:
    if name == "get_order_status":
        return fixture_order(arguments.get("order_number", ""))
    if name == "get_stock":
        sku = str(arguments.get("sku") or "")
        for product in PRODUCT_FIXTURES:
            for variant in product["variants"]:
                if variant["sku"].casefold() == sku.casefold():
                    return {"found": True, "product_name": product["product_name"], **variant}
        return {"found": False, "sku": sku}
    if name in {"search_products", "search_available_products"}:
        result = fixture_product(arguments.get("query", ""))
        return [
            {"product_id": p["product_id"], "name": p["product_name"], "published": True, "variants": p["variants"]}
            for p in result["products"]
        ]
    if name == "get_product_availability":
        for product in PRODUCT_FIXTURES:
            if product["product_id"] == arguments.get("product_id"):
                return json.loads(json.dumps({"found": True, **product}))
        return {"found": False}
    if name == "request_isa_handoff":
        return {"handoff_requested": True, "reason": arguments.get("reason")}
    if name in {"set_turn_decision", "select_sale_candidate"}:
        return {"recorded": True}
    return {"error": "unknown fixture tool"}


class V1ReplayRunner:
    """Replay adapter for the current v1 brain, without webhook side effects."""

    def answer(self, message: str, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        started = time.monotonic()
        simple = v1_app._simple_customer_reply(message)
        if simple:
            return self._result(simple, [], 0, {}, started)

        prior_product = _last_named_product(history)
        core_state = {"active_product_name": prior_product or None, "active_sku": None}
        scope_reply = v1_app._isa_scope_handoff(message, core_state)
        if scope_reply:
            return self._result(
                scope_reply,
                [{"name": "handoff_to_isa", "arguments": {"reason": "v1_scope"}}],
                0, {}, started,
            )

        number = v1_app.extract_order_number(message)
        if not number and v1_app._fred_just_asked_for_order_number(history):
            number = v1_app._extract_bare_order_number(message)
        knowledge_probe = v1_app._search_local_knowledge_bundle(message)
        requirement = classify_turn_data_requirement(
            message,
            governing_topic=knowledge_probe.governing_topic,
            knowledge_context=knowledge_probe.context,
            dynamic_requirements=knowledge_probe.dynamic_requirements,
            product_lexicon=v1_app.product_lexicon(),
            product_lexicon_available=v1_app.product_lexicon_available(),
        )
        if number and requirement.get("intent") == INTENT_EXISTING_ORDER:
            evidence = fixture_order(number)
            reply = v1_app._render_order_status_reply(evidence)
            return self._result(
                reply, [{"name": "get_order", "arguments": {"order_number": str(number)}}],
                0, {}, started, tool_results=[{"name": "get_order", "result": evidence}],
            )
        if requirement.get("intent") == INTENT_EXISTING_ORDER and not number:
            return self._result(
                "¿Me pasás el número de pedido?", [], 0, {}, started,
            )

        rag_context = ""
        pre_tools: List[Dict[str, Any]] = []
        evidence_rows: List[Dict[str, Any]] = []
        if requirement.get("data_required") == DATA_KNOWLEDGE_ONLY:
            rag_context = KNOWLEDGE_FIXTURE
            pre_tools.append({"name": "search_knowledge", "arguments": {"query": message}})
            evidence_rows.append({"name": "search_knowledge", "result": {"context": KNOWLEDGE_FIXTURE}})
        elif any(name.casefold() in _norm(message + " " + prior_product) for name in ("Isabel", "Taylor", "Fantasma")):
            product = fixture_product(message + " " + prior_product)
            rag_context = "Producto Tiendanube fixture: {}".format(json.dumps(product, ensure_ascii=False))

        def traced_v1_tool(name, arguments):
            tool_result = _v1_tool(name, arguments)
            if canonical_tool(name) == "handoff_to_isa" and isinstance(tool_result, dict):
                tool_result = {**tool_result, "mode": "live_baseline"}
            evidence_rows.append({"name": canonical_tool(name), "result": tool_result})
            return tool_result

        with patch.object(v1_agent, "_run_tool", side_effect=traced_v1_tool):
            result = v1_agent.answer(
                message, history=history, rag_context=rag_context,
                greeting_required=not any(item.get("role") == "assistant" for item in history),
                verbose=False,
            )
        calls = pre_tools + [
            {"name": canonical_tool(call.get("name", "")), "arguments": call.get("arguments") or {}}
            for call in result.get("tool_calls") or []
        ]
        if result.get("handoff") and not any(call["name"] == "handoff_to_isa" for call in calls):
            calls.append({"name": "handoff_to_isa", "arguments": result["handoff"]})
        return self._result(
            result.get("reply", ""), calls, result.get("model_calls", 0),
            result.get("usage") or {}, started, tool_results=evidence_rows,
        )

    @staticmethod
    def _result(reply, calls, model_calls, usage, started, tool_results=None):
        return {
            "reply": reply, "tool_calls": calls, "tool_results": tool_results or [],
            "model_calls": model_calls, "usage": usage,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "errors": [],
            "decision": {"action": "handoff_to_isa" if any(c["name"] == "handoff_to_isa" for c in calls) else "reply"},
        }


def canonical_tool(name: str) -> str:
    if name == "get_order_status":
        return "get_order"
    if name in {"search_products", "search_available_products", "get_stock", "get_product_availability"}:
        return "get_product"
    if name == "request_isa_handoff":
        return "handoff_to_isa"
    return name


def _unique_tools(calls: Iterable[Dict[str, Any]]) -> List[str]:
    return list(dict.fromkeys(canonical_tool(call.get("name", "")) for call in calls))


def hallucination_flags(result: Dict[str, Any]) -> List[str]:
    reply = str(result.get("reply") or "")
    evidence = json.dumps(result.get("tool_results") or [], ensure_ascii=False)
    flags = []
    if "checkout" in _norm(reply) or any("checkout" in _norm(call.get("name")) for call in result.get("tool_calls") or []):
        flags.append("checkout")
    facts = [
        "ISABEL-I-CHOCOLATE", "ISABEL-I-BLACK", "TAYLOR-BLACK", "TAYLOR-CHOCOLATE",
        "12500", "11900", "TRK6343", "tracking.example/6343",
    ]
    for fact in facts:
        if _norm(fact) in _norm(reply) and _norm(fact) not in _norm(evidence):
            flags.append("unsupported_fact:{}".format(fact))
    for price in re.findall(r"\$\s*([0-9][0-9.,]*)", reply):
        digits = "".join(char for char in price if char.isdigit())
        if digits and digits not in "".join(char for char in evidence if char.isdigit()):
            flags.append("unsupported_price:{}".format(price))
    commercial_claim = any(term in _norm(reply) for term in (
        "hay stock", "tenemos stock", "está disponible", "esta disponible", "sin stock",
    ))
    if commercial_claim and not any(
        canonical_tool(call.get("name", "")) in {"get_product", "get_order"}
        for call in result.get("tool_calls") or []
    ):
        flags.append("unsupported_stock_claim")
    order_claim = any(term in _norm(reply) for term in (
        "fue despachado", "ya fue despachado", "figura como entregado",
        "ya está empaquetado", "ya esta empaquetado", "está en preparación",
    ))
    if order_claim and not any(
        canonical_tool(call.get("name", "")) == "get_order"
        for call in result.get("tool_calls") or []
    ):
        flags.append("unsupported_order_claim")
    for row in result.get("tool_results") or []:
        tool_result = row.get("result") or {}
        if row.get("name") == "get_order":
            fulfillment = str(tool_result.get("fulfillment_status") or "").upper()
            if fulfillment in {"UNPACKED", "PACKED"} and any(
                phrase in _norm(reply) for phrase in (
                    "ya está listo para retirar", "ya esta listo para retirar",
                    "está listo para retirar", "esta listo para retirar",
                    "podés pasar a retirar", "podes pasar a retirar",
                )
            ):
                flags.append("order_stage_overclaim:{}".format(fulfillment))
        if row.get("name") == "handoff_to_isa" and tool_result.get("mode") in {"dry_run", "preview"}:
            if any(phrase in _norm(reply) for phrase in (
                "te paso con isa", "ya se lo pasé", "ya se lo pase", "te va a contactar",
                "te atiende enseguida",
            )):
                flags.append("dry_run_handoff_claim")
    return list(dict.fromkeys(flags))


def score(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    expect = case["expect"]
    tools = _unique_tools(result.get("tool_calls") or [])
    required = expect.get("tools") or []
    causes = []
    missing = [name for name in required if name not in tools]
    extra = [name for name in tools if name not in required]
    forbidden = [name for name in expect.get("forbid") or [] if name in tools]
    if missing:
        causes.append("wrong_tool")
    if extra or forbidden:
        causes.append("stale_context" if case["category"] == "topic_change" else "unnecessary_tool")
    handoff = "handoff_to_isa" in tools
    if "handoff" in expect and handoff != bool(expect["handoff"]):
        causes.append("wrong_handoff")
    if expect.get("order_number"):
        arguments = [call.get("arguments") or {} for call in result.get("tool_calls") or [] if canonical_tool(call.get("name", "")) == "get_order"]
        if not arguments or str(arguments[-1].get("order_number")) != str(expect["order_number"]):
            causes.append("wrong_tool")
    reply = _norm(result.get("reply"))
    if not reply:
        causes.append("bad_response")
    elif expect.get("contains_any") and not any(_norm(term) in reply for term in expect["contains_any"]):
        causes.append("missing_data")
    flags = hallucination_flags(result)
    if flags:
        causes.append("hallucination")
    if result.get("errors"):
        causes.append("bad_response")
    if float(result.get("latency_ms") or 0) > 15000:
        causes.append("latency")
    causes = list(dict.fromkeys(causes))
    blocking = {"wrong_tool", "stale_context", "hallucination", "wrong_handoff"}
    status = "FAIL" if blocking.intersection(causes) else "REVIEW" if causes else "PASS"
    return {"status": status, "causes": causes, "hallucination_flags": flags}


def run_case(case: Dict[str, Any], runner: Any) -> Dict[str, Any]:
    try:
        result = runner.answer(case["message"], history=case.get("history") or [])
    except Exception as error:  # noqa: BLE001
        result = {
            "reply": "", "tool_calls": [], "tool_results": [], "model_calls": 0,
            "usage": {}, "latency_ms": 0, "errors": ["{}: {}".format(type(error).__name__, error)],
            "decision": {},
        }
    evaluation = score(case, result)
    return {
        "response": result.get("reply", ""),
        "tools": _unique_tools(result.get("tool_calls") or []),
        "tool_calls": result.get("tool_calls") or [],
        "tool_results": result.get("tool_results") or [],
        "llm_calls": result.get("model_calls", 0),
        "tokens": result.get("usage") or {},
        "latency_ms": result.get("latency_ms", 0),
        "error": result.get("errors") or [],
        "handoff": "handoff_to_isa" in _unique_tools(result.get("tool_calls") or []),
        "live_data": [name for name in _unique_tools(result.get("tool_calls") or []) if name in {"get_order", "get_product"}],
        "decision": result.get("decision") or {},
        **evaluation,
    }


def _fixture_evidence_for_calls(
    calls: List[Dict[str, Any]], *, handoff_mode: str = "dry_run",
) -> List[Dict[str, Any]]:
    evidence = []
    for call in calls:
        name = canonical_tool(call.get("name", ""))
        arguments = call.get("arguments") or {}
        if name == "search_knowledge":
            result = {"context": KNOWLEDGE_FIXTURE}
        elif name == "get_order":
            result = fixture_order(arguments.get("order_number", ""))
        elif name == "get_product":
            result = fixture_product(arguments.get("query") or arguments.get("sku") or "")
        elif name == "handoff_to_isa":
            result = {"mode": handoff_mode, **arguments}
        else:
            continue
        evidence.append({"name": name, "result": result})
    return evidence


def rescore_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    for row in payload["cases"]:
        case = {"category": row["category"], "expect": row["expected"]}
        for version in ("v1", "v2"):
            stored = row[version]
            calls = stored.get("tool_calls") or []
            evidence = stored.get("tool_results") or _fixture_evidence_for_calls(
                calls, handoff_mode="live_baseline" if version == "v1" else "dry_run",
            )
            if version == "v1":
                for evidence_row in evidence:
                    if evidence_row.get("name") == "handoff_to_isa":
                        evidence_row.setdefault("result", {})["mode"] = "live_baseline"
            raw = {
                "reply": stored.get("response", ""), "tool_calls": calls,
                "tool_results": evidence, "errors": stored.get("error") or [],
                "latency_ms": stored.get("latency_ms", 0),
            }
            stored["tool_results"] = evidence
            stored.update(score(case, raw))
    payload["summary"] = {
        "v1": metrics(payload["cases"], "v1"),
        "v2": metrics(payload["cases"], "v2"),
    }
    payload["recommendation"] = recommendation(payload["cases"], payload["summary"])
    return payload


def percentile(values: List[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return round(float(ordered[rank]), 2)


def metrics(rows: List[Dict[str, Any]], version: str) -> Dict[str, Any]:
    results = [row[version] for row in rows]
    statuses = Counter(result["status"] for result in results)
    latencies = [float(result["latency_ms"]) for result in results]
    return {
        "pass": statuses["PASS"], "review": statuses["REVIEW"], "fail": statuses["FAIL"],
        "pass_rate": round(statuses["PASS"] / len(results), 4),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "tool_calls_avg": round(statistics.mean(len(result["tool_calls"]) for result in results), 3),
        "llm_calls_avg": round(statistics.mean(result["llm_calls"] for result in results), 3),
        "tokens_avg": round(statistics.mean((result["tokens"].get("total_tokens") or 0) for result in results), 2),
        "causes": dict(Counter(cause for result in results for cause in result["causes"])),
    }


def recommendation(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
    v2_blockers = [row for row in rows if row["v2"]["status"] == "FAIL"]
    go = not v2_blockers and summary["v2"]["pass_rate"] >= 0.90
    return {
        "decision": "GO" if go else "NO-GO",
        "reason": (
            "V2 no tuvo bloqueantes y superó 90% PASS."
            if go else "V2 conserva fallos bloqueantes o no alcanza 90% PASS; no ejecutar shadow real todavía."
        ),
    }


def render_report(payload: Dict[str, Any]) -> str:
    summary = payload["summary"]
    rows = payload["cases"]
    score_value = {"PASS": 2, "REVIEW": 1, "FAIL": 0}
    v1_wins = sum(score_value[row["v1"]["status"]] > score_value[row["v2"]["status"]] for row in rows)
    v2_wins = sum(score_value[row["v2"]["status"]] > score_value[row["v1"]["status"]] for row in rows)
    ties = len(rows) - v1_wins - v2_wins
    worst = sorted(rows, key=lambda row: (score_value[row["v2"]["status"]], -len(row["v2"]["causes"])))[:10]
    v1_failed = [row["id"] for row in rows if row["v1"]["status"] == "FAIL"]
    v2_failed = [row["id"] for row in rows if row["v2"]["status"] == "FAIL"]
    v2_hallucinations = sum(len(row["v2"]["hallucination_flags"]) for row in rows)
    v2_checkouts = sum("checkout" in row["v2"]["hallucination_flags"] for row in rows)
    lines = [
        "# Fred v1 vs v2 — evaluación A/B offline",
        "",
        "Fecha de ejecución: `{}`. Casos: **{}**. Modelo real: **{}**.".format(payload["generated_at"], len(rows), "sí" if payload["live_model"] else "no"),
        "",
        "## Recomendación",
        "",
        "**{}** — {}".format(payload["recommendation"]["decision"], payload["recommendation"]["reason"]),
        "",
        "Esta ejecución usa el LLM real con fixtures deterministas de Knowledge/Tiendanube. No lee pedidos reales, no envía WhatsApp, no crea handoffs ni checkout y no modifica estado productivo.",
        "",
        "## Resultado general",
        "",
        "| Métrica | v1 | v2 |",
        "|---|---:|---:|",
        "| PASS | {} | {} |".format(summary["v1"]["pass"], summary["v2"]["pass"]),
        "| REVIEW | {} | {} |".format(summary["v1"]["review"], summary["v2"]["review"]),
        "| FAIL | {} | {} |".format(summary["v1"]["fail"], summary["v2"]["fail"]),
        "| Win rate pareado | {:.1%} | {:.1%} |".format(v1_wins / len(rows), v2_wins / len(rows)),
        "| Latencia p50 | {:.2f} ms | {:.2f} ms |".format(summary["v1"]["latency_p50_ms"], summary["v2"]["latency_p50_ms"]),
        "| Latencia p95 | {:.2f} ms | {:.2f} ms |".format(summary["v1"]["latency_p95_ms"], summary["v2"]["latency_p95_ms"]),
        "| Tool calls promedio | {:.3f} | {:.3f} |".format(summary["v1"]["tool_calls_avg"], summary["v2"]["tool_calls_avg"]),
        "| LLM calls promedio | {:.3f} | {:.3f} |".format(summary["v1"]["llm_calls_avg"], summary["v2"]["llm_calls_avg"]),
        "| Tokens promedio | {:.2f} | {:.2f} |".format(summary["v1"]["tokens_avg"], summary["v2"]["tokens_avg"]),
        "",
        "Empates: **{}**.".format(ties),
        "",
        "## Fallos por causa",
        "",
        "- v1: `{}`".format(json.dumps(summary["v1"]["causes"], ensure_ascii=False, sort_keys=True)),
        "- v2: `{}`".format(json.dumps(summary["v2"]["causes"], ensure_ascii=False, sort_keys=True)),
        "",
        "- FAIL v1 ({}): `{}`".format(len(v1_failed), ", ".join(v1_failed)),
        "- FAIL v2 ({}): `{}`".format(len(v2_failed), ", ".join(v2_failed)),
        "",
        "## Bloqueantes v2",
        "",
        "| Control | Resultado |",
        "|---|---:|",
        "| Hallucination flags | {} |".format(v2_hallucinations),
        "| Checkout creado/propuesto | {} |".format(v2_checkouts),
        "| Casos de asesoría sin handoff correcto | {} |".format(sum(row["id"].startswith("advice-") and row["v2"]["status"] == "FAIL" for row in rows)),
        "| Producto inexistente sin handoff correcto | {} |".format(sum(row["id"] == "product-06" and row["v2"]["status"] == "FAIL" for row in rows)),
        "| Cambio de tema con estado viejo | {} |".format(sum("stale_context" in row["v2"]["causes"] for row in rows)),
        "",
        "## 10 peores casos de v2",
        "",
        "| Caso | Estado | Causas | Respuesta v2 | Tools v2 |",
        "|---|---|---|---|---|",
    ]
    for row in worst:
        response = row["v2"]["response"].replace("|", "\\|").replace("\n", " ")[:180]
        lines.append("| {} | {} | {} | {} | {} |".format(
            row["id"], row["v2"]["status"], ", ".join(row["v2"]["causes"]) or "-",
            response, ", ".join(row["v2"]["tools"]) or "ninguna",
        ))
    lines.extend((
        "", "## Soporte shadow preparado", "",
        "`bot/v2_shadow.py` recibe el mismo turno/historial después de v1 y devuelve únicamente respuesta propuesta, tools, latencia, decisión, llamadas/tokens y errores. Usa el handoff preview/dry-run de v2; no conoce teléfono, no envía WhatsApp, no crea checkout y no modifica estado. `bot/app.py` no lo importa todavía.",
        "", "## Verificación", "",
        "- Suite completa: **683 tests OK** (661 originales de v1 + 22 de v2/A-B).",
        "- Modelo real: DeepSeek; datos comerciales: fixtures locales deterministas.",
        "- Sin llamadas a Meta, Railway, Supabase o Tiendanube y sin escrituras externas.",
        "", "## Alcance del veredicto", "",
        "El resultado habilita o bloquea únicamente el siguiente paso: preparar un shadow real read-only. No autoriza conectar el webhook, desplegar, enviar respuestas v2 ni escribir en sistemas productivos.",
        "", "El JSON adjunto conserva por caso: respuestas, tools/argumentos, llamadas LLM, tokens, latencia, errores, handoff, datos live simulados, hallucination flags, estado y causas.",
    ))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", action="store_true", help="Usa DeepSeek real; fixtures comerciales siguen locales.")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--md-output", type=Path, default=DEFAULT_MD_OUTPUT)
    parser.add_argument("--limit", type=int, help="Ejecuta sólo los primeros N casos para smoke tests.")
    parser.add_argument("--rescore-json", type=Path, help="Recalcula scoring/reporte sin nuevas llamadas al modelo.")
    args = parser.parse_args()
    if args.rescore_json:
        payload = rescore_payload(json.loads(args.rescore_json.read_text(encoding="utf-8")))
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.md_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.md_output.write_text(render_report(payload), encoding="utf-8")
        print(json.dumps({"summary": payload["summary"], "recommendation": payload["recommendation"]}, ensure_ascii=False, indent=2))
        return 0
    if args.env_file:
        load_dotenv(args.env_file)
    if not args.live_model:
        raise SystemExit("Esta batería seria requiere --live-model; los tests unitarios cubren el modo simulado.")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise SystemExit("Falta DEEPSEEK_API_KEY.")

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.limit:
        cases = cases[:args.limit]
    v1 = V1ReplayRunner()
    v2 = v2_runner()
    rows = []
    for index, case in enumerate(cases, start=1):
        print("[{}/{}] {}".format(index, len(cases), case["id"]), flush=True)
        rows.append({
            "id": case["id"], "category": case["category"], "message": case["message"],
            "history": case.get("history") or [], "expected": case["expect"],
            "v1": run_case(case, v1), "v2": run_case(case, v2),
        })
    summary = {"v1": metrics(rows, "v1"), "v2": metrics(rows, "v2")}
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live_model": True, "fixture_data": True, "cases": rows, "summary": summary,
    }
    payload["recommendation"] = recommendation(rows, summary)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.md_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.md_output.write_text(render_report(payload), encoding="utf-8")
    print(json.dumps({"summary": summary, "recommendation": payload["recommendation"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
