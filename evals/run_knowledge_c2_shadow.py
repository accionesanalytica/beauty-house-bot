"""C4 shadow: C3 decisions plus a presentation-only WhatsApp style layer.

Allowed external effects are read-only GET requests performed by Fred's current
catalog/order tools, Supabase knowledge reads when explicitly selected, and
calls to the configured language model. It never starts FastAPI, so startup
jobs, WhatsApp, checkout and order creation remain disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv


PROJECT = Path(__file__).resolve().parents[1]
BOT = PROJECT / "bot"
sys.path.insert(0, str(BOT))
load_dotenv(PROJECT / ".env")

import agent  # noqa: E402
from knowledge_rag import (  # noqa: E402
    _disclosure_present as obligation_disclosure_present,
    enforce_knowledge_obligations,
    load_knowledge_chunks,
    retrieve_with_recent_context,
    retrieve_local_knowledge,
)
from dynamic_checks import (  # noqa: E402
    execute_dynamic_requirements,
    format_dynamic_check_context,
)
from conversation_quality import apply_conversation_contract  # noqa: E402
from routing_policy import (  # noqa: E402
    align_reply_with_routing,
    resolve_harness_routing,
    visible_routing_contract,
)


CASES_PATH = PROJECT / "evals" / "knowledge_v1_cases.jsonl"
KNOWLEDGE_PATH = PROJECT / "knowledge"
SAFE_READ_ONLY_TOOLS = {
    "search_products", "search_available_products", "get_stock",
    "get_product_availability", "get_order_status",
}
INTERNAL_NO_WRITE_TOOLS = {
    "request_isa_handoff", "select_sale_candidate", "set_turn_decision",
}


def assert_shadow_is_read_only() -> None:
    exposed = set(agent.AVAILABLE_TOOLS)
    unsafe = exposed - SAFE_READ_ONLY_TOOLS
    if unsafe:
        raise RuntimeError("Shadow abortado: tools externas no allowlisted: {}".format(sorted(unsafe)))
    schema_names = {
        schema["function"]["name"] for schema in agent.TOOL_SCHEMAS
    }
    unknown = schema_names - SAFE_READ_ONLY_TOOLS - INTERNAL_NO_WRITE_TOOLS
    if unknown:
        raise RuntimeError("Shadow abortado: schemas no aislados: {}".format(sorted(unknown)))


def load_cases() -> List[Dict[str, Any]]:
    return [
        case for case in (
            json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if case.get("scope") == "knowledge"
    ]


def add_usage(counter: Counter, usage: Dict[str, Any]) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            counter[key] += value


def call_judge(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("Falta DEEPSEEK_API_KEY")
    response = requests.post(
        agent.DEEPSEEK_URL,
        headers={"Authorization": "Bearer {}".format(api_key), "Content-Type": "application/json"},
        json={
            "model": agent.MODEL,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return {
        "assessment": json.loads(payload["choices"][0]["message"]["content"]),
        "usage": payload.get("usage") or {},
    }


def judge_case(case: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    rubric = """Evaluás una conversación shadow de Fred, asistente de Beauty House.
No premies verbosidad. Evaluá únicamente la evidencia incluida.

Separá dos métricas de 0 a 100:
1. knowledge_correctness: hechos correctos, retrieval pertinente, routing resolve/escalate,
   uso correcto de herramientas disponibles, disclosures/links obligatorios, y cero datos
   dinámicos inventados. Si un live check pedido no tiene herramienta disponible, es correcto
   reconocer el límite o escalar; es incorrecto inventar el resultado.
2. conversation_quality: naturalidad, concisión, utilidad, ausencia de repetición,
   preguntas innecesarias, datos ya dados que se vuelven a pedir, o empuje prematuro a compra.
   La respuesta debe contestar primero, sumar un próximo paso sólo si hace falta,
   pedir un solo dato faltante por vez y sonar como WhatsApp, no como un documento.

Clasificá outcome como PASS, PARTIAL o FAIL. Señalá problemas concretos, no hipotéticos.
Respondé JSON válido con esta forma exacta:
{
  "outcome": "PASS|PARTIAL|FAIL",
  "knowledge_correctness": 0,
  "conversation_quality": 0,
  "dimensions": {
    "factual_correctness": 1, "retrieval_quality": 1, "tool_use": 1,
    "routing": 1, "required_obligations": 1, "no_dynamic_invention": 1,
    "naturalness": 1, "conciseness": 1, "no_repetition": 1,
    "no_unnecessary_questions": 1, "no_reasking_known_data": 1,
    "no_premature_sales_push": 1
  },
  "findings": ["..."], "good": ["..."]
}
Cada dimension va de 1 (mal) a 5 (excelente)."""
    return call_judge([
        {"role": "system", "content": rubric},
        {"role": "user", "content": json.dumps({"case": case, **evidence}, ensure_ascii=False)},
    ])


def retrieval_trace(retrieval: Any, turn: int, query: str) -> Dict[str, Any]:
    return {
        "turn": turn,
        "query": query,
        "retrieved_topics": list(retrieval.retrieved_topics),
        "governing_topic": retrieval.governing_topic,
        "governing_reason": retrieval.governing_reason,
        "topic_scores": list(retrieval.topic_scores),
        "sources": [row.get("source_id") for row in retrieval.rows],
        "chunks": [
            {
                "source_id": row.get("source_id"),
                "topic": (row.get("metadata") or {}).get("topic"),
                "score": row.get("similarity"),
                "excerpt": re.sub(r"\s+", " ", str(row.get("content") or "")).strip()[:500],
            }
            for row in retrieval.rows
        ],
        "obligations_applied": {
            "required_disclosures": [item.get("id") for item in retrieval.obligations.required_disclosures],
            "required_links": [item.get("id") for item in retrieval.obligations.required_links],
            "escalation_required": retrieval.obligations.escalation_required,
        },
        "obligation_details": {
            "required_disclosures": [dict(item) for item in retrieval.obligations.required_disclosures],
            "required_links": [dict(item) for item in retrieval.obligations.required_links],
        },
        "obligations_discarded": list(retrieval.discarded_obligations),
        "dynamic_requirements": [
            {
                "fact": item.fact, "verifier": item.verifier, "status": item.status,
                "required_arguments": list(item.required_arguments),
                "missing_arguments": list(item.missing_arguments),
                "arguments": dict(item.arguments), "fallback": item.fallback,
            }
            for item in retrieval.dynamic_requirements
        ],
    }


def run_case(case: Dict[str, Any], chunks: List[Any], knowledge_source: str) -> Dict[str, Any]:
    history: List[Dict[str, str]] = []
    transcript: List[Dict[str, str]] = []
    retrievals: List[Dict[str, Any]] = []
    tools: List[Dict[str, Any]] = []
    outcomes: List[Dict[str, Any]] = []
    total_usage = Counter()

    if knowledge_source == "supabase":
        import app as production_app
        production_app.KNOWLEDGE_RAG_ENABLED = True
        production_app.KNOWLEDGE_RAG_SOURCE = "supabase"

        def retrieve(query: str):
            return production_app.search_knowledge_bundle(query, limit=6)
    else:
        def retrieve(query: str):
            return retrieve_local_knowledge(query, chunks, limit=6)

    for turn, customer_message in enumerate(case["messages"], start=1):
        retrieval, retrieval_query, used_conversation_fallback = (
            retrieve_with_recent_context(customer_message, history, retrieve)
        )
        trace = retrieval_trace(retrieval, turn, retrieval_query)
        trace["current_message"] = customer_message
        trace["conversation_fallback_used"] = used_conversation_fallback
        dynamic_checks = execute_dynamic_requirements(
            retrieval.dynamic_requirements,
            agent.AVAILABLE_TOOLS,
        )
        dynamic_context = format_dynamic_check_context(dynamic_checks)
        rag_context = "\n\n".join(
            item for item in (retrieval.context, dynamic_context) if item
        )
        result = agent.answer(
            customer_message,
            history=history,
            rag_context=rag_context,
            greeting_required=not any(item["role"] == "assistant" for item in history),
            verbose=False,
        )
        pre_route = None

        reply = str(result.get("reply") or "")
        routing = resolve_harness_routing(
            customer_message, history,
            decision=result.get("decision") or {},
            handoff=result.get("handoff"),
            knowledge_retrieval=retrieval,
            dynamic_requirements=dynamic_checks,
        )
        reply = align_reply_with_routing(
            reply,
            routing,
            dynamic_requirements=dynamic_checks,
        )
        reply = enforce_knowledge_obligations(
            reply, retrieval.obligations, verified_dynamic_links=[],
        )
        reply = apply_conversation_contract(
            reply,
            history=history,
            routing_contract=visible_routing_contract(
                routing,
                dynamic_requirements=dynamic_checks,
            ),
        )
        trace["obligation_delivery"] = {
            "required_disclosures": {
                str(item.get("id") or "unnamed"): obligation_disclosure_present(reply, item)
                for item in retrieval.obligations.required_disclosures
            },
            "required_links": {
                str(item.get("id") or "unnamed"): str(item.get("url") or "") in reply
                for item in retrieval.obligations.required_links
            },
        }
        transcript.extend([
            {"role": "customer", "content": customer_message},
            {"role": "fred", "content": reply},
        ])
        history.extend([
            {"role": "user", "content": customer_message},
            {"role": "assistant", "content": reply},
        ])
        tools.extend({"turn": turn, **tool} for tool in (result.get("tool_calls") or []))
        tools.extend({
            "turn": turn,
            "name": item.verifier,
            "arguments": dict(item.arguments),
            "source": "deterministic_dynamic_check",
            "status": item.status,
            "result": dict(item.result),
            "error": item.error,
        } for item in dynamic_checks)
        outcomes.append({
            "turn": turn,
            "pre_route": pre_route,
            "agent_decision": result.get("decision") or {},
            "effective_decision": routing["decision"],
            "effective_handoff": routing["handoff"],
            "routing_source": routing["source"],
            "visible_routing_contract_applied": True,
            "dynamic_checks": [
                {
                    "fact": item.fact,
                    "verifier": item.verifier,
                    "status": item.status,
                    "arguments": dict(item.arguments),
                    "result": dict(item.result),
                    "error": item.error,
                }
                for item in dynamic_checks
            ],
            "sale_candidate": result.get("sale_candidate"),
            "commercial_trace": result.get("commercial_trace"),
            "model_calls": result.get("model_calls"),
        })
        retrievals.append(trace)
        add_usage(total_usage, result.get("usage") or {})

    evidence = {
        "expected": {
            "outcome": case.get("expected_outcome"),
            "topics": case.get("expected_topics") or [],
            "required_live_checks": case.get("required_live_checks") or [],
            "disclosures": case.get("expected_disclosures") or [],
            "links": case.get("expected_links") or [],
        },
        "transcript": transcript,
        "retrievals": retrievals,
        "tool_calls": tools,
        "agent_outcomes": outcomes,
    }
    judged = judge_case(case, evidence)
    add_usage(total_usage, judged.get("usage") or {})
    return {
        "id": case["id"], **evidence,
        "score": judged["assessment"], "usage": dict(total_usage),
    }


def summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [row for row in results if "score" in row]
    outcomes = Counter(row["score"].get("outcome", "UNKNOWN") for row in valid)
    usage = Counter()
    for row in valid:
        add_usage(usage, row.get("usage") or {})
    fred_messages = [
        message["content"]
        for row in valid
        for message in row.get("transcript", [])
        if message.get("role") == "fred"
    ]
    normalized_repetitions = 0
    for row in valid:
        replies = [
            " ".join(re.sub(r"[^a-z0-9]+", " ", message["content"].lower()).split())
            for message in row.get("transcript", [])
            if message.get("role") == "fred"
        ]
        normalized_repetitions += sum(
            current == previous and bool(current)
            for previous, current in zip(replies, replies[1:])
        )
    return {
        "cases": len(valid), "outcomes": dict(outcomes),
        "knowledge_correctness_avg": round(sum(float(row["score"].get("knowledge_correctness", 0)) for row in valid) / len(valid), 1) if valid else 0,
        "conversation_quality_avg": round(sum(float(row["score"].get("conversation_quality", 0)) for row in valid) / len(valid), 1) if valid else 0,
        "average_reply_words": round(
            sum(len(re.findall(r"\w+", message)) for message in fred_messages) / len(fred_messages), 1
        ) if fred_messages else 0,
        "replies_ending_in_question": sum(
            message.rstrip().endswith("?") for message in fred_messages
        ),
        "replies_containing_question": sum(
            bool(re.search(r"¿[^?]+\?", message)) for message in fred_messages
        ),
        "exact_consecutive_repetitions": normalized_repetitions,
        "judge_unnecessary_question_issues": sum(
            float(row["score"].get("dimensions", {}).get("no_unnecessary_questions", 5)) < 4
            for row in valid
        ),
        "judge_repetition_issues": sum(
            float(row["score"].get("dimensions", {}).get("no_repetition", 5)) < 4
            for row in valid
        ),
        "usage": dict(usage),
    }


def c3_comparison(results: List[Dict[str, Any]], c3_path: str) -> List[Dict[str, Any]]:
    if not Path(c3_path).exists():
        return []
    c3 = {row["id"]: row for row in json.loads(Path(c3_path).read_text())["results"]}
    comparison = []
    for row in results:
        before = c3.get(row["id"], {}).get("score", {})
        after = row.get("score", {})
        comparison.append({
            "id": row["id"],
            "c3": before.get("outcome"), "c4": after.get("outcome"),
            "knowledge_c3": before.get("knowledge_correctness"),
            "knowledge_c4": after.get("knowledge_correctness"),
            "quality_c3": before.get("conversation_quality"),
            "quality_c4": after.get("conversation_quality"),
        })
    return comparison


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/private/tmp/fred-c4-shadow-report.json")
    parser.add_argument("--c3", default="/private/tmp/fred-c3-shadow-report.json")
    parser.add_argument("--limit", type=int, default=19)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--knowledge-source", choices=("local", "supabase"), default="local")
    args = parser.parse_args()
    assert_shadow_is_read_only()
    chunks = load_knowledge_chunks(KNOWLEDGE_PATH)
    results = []
    selected_cases = load_cases()[args.start:args.limit]
    for index, case in enumerate(selected_cases, start=args.start + 1):
        print("[{}/{}] {}".format(index, args.limit, case["id"]), flush=True)
        try:
            results.append(run_case(case, chunks, args.knowledge_source))
        except Exception as error:
            results.append({"id": case["id"], "error": "{}: {}".format(type(error).__name__, str(error)[:1000])})
        report = {
            "mode": "c4-conversation-quality/read-only-shadow/{}".format(args.knowledge_source),
            "safety": {
                "writes_disabled": ["Supabase", "WhatsApp", "checkout", "orders", "stock", "Meta", "Railway"],
                "external_tools_allowlisted": sorted(SAFE_READ_ONLY_TOOLS),
            },
            "results": results,
        }
        report["summary"] = summary(results)
        report["comparison"] = c3_comparison(results, args.c3)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
