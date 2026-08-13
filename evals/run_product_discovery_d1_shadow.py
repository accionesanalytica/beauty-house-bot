"""D1 shadow: baseline for product discovery vs. lookup routing.

Reuses the same pieces as evals/run_knowledge_c2_shadow.py (agent.answer +
knowledge_rag + dynamic_checks + routing_policy), plus the catalog/Tiendanube
layer that only exists in bot/app.py (_catalog_retrieval_query,
search_similar_products, _live_candidate_context). Importing app.py only
registers FastAPI routes in memory; it never calls uvicorn.run (that is
gated behind `if __name__ == "__main__"`), so this never starts a server,
never touches Meta/WhatsApp, and never enables the M1 durable flag on its own.

Allowed external effects: read-only GET-style tool calls (search_products,
get_stock, get_product_availability), Supabase knowledge reads, and calls to
the configured language model. No writes, no checkout, no WhatsApp, no M1.

Usage:
    python evals/run_product_discovery_d1_shadow.py --live --allow-large-batch
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT = Path(__file__).resolve().parents[1]
BOT = PROJECT / "bot"
sys.path.insert(0, str(BOT))

import agent  # noqa: E402
from knowledge_rag import retrieve_with_recent_context, retrieve_local_knowledge  # noqa: E402
from dynamic_checks import execute_dynamic_requirements, format_dynamic_check_context  # noqa: E402
from routing_policy import align_reply_with_routing, resolve_harness_routing  # noqa: E402

CASES_PATH = PROJECT / "evals" / "product_discovery_d1_cases.jsonl"
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
        raise RuntimeError("Shadow abortado: tools no allowlisted: {}".format(sorted(unsafe)))
    schema_names = {schema["function"]["name"] for schema in agent.TOOL_SCHEMAS}
    unknown = schema_names - SAFE_READ_ONLY_TOOLS - INTERNAL_NO_WRITE_TOOLS
    if unknown:
        raise RuntimeError("Shadow abortado: schemas no aislados: {}".format(sorted(unknown)))


def load_cases() -> List[Dict[str, Any]]:
    return [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run_turn(app_module, message_text: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
    """Mirror bot/app.py's real turn pipeline (catalog + knowledge + agent + routing),
    without persistence, WhatsApp sending, or the durable queue."""
    catalog_query = app_module._catalog_retrieval_query(message_text, history)
    catalog_context = app_module.search_similar_products(catalog_query)
    live_candidate_context = app_module._live_candidate_context(catalog_context, catalog_query)
    if live_candidate_context:
        catalog_context = "{}\n\n{}".format(catalog_context, live_candidate_context)

    knowledge_context = ""
    dynamic_checks = ()
    retrieval = None
    if app_module.KNOWLEDGE_RAG_ENABLED:
        def retrieve(query: str):
            return app_module.search_knowledge_bundle(query, limit=6)

        retrieval, _, _ = retrieve_with_recent_context(message_text, history, retrieve)
        dynamic_checks = execute_dynamic_requirements(
            retrieval.dynamic_requirements, agent.AVAILABLE_TOOLS,
        )
        dynamic_context = format_dynamic_check_context(dynamic_checks)
        knowledge_context = "\n\n".join(
            item for item in (retrieval.context, dynamic_context) if item
        )

    rag_context = "\n\n".join(item for item in (catalog_context, knowledge_context) if item)

    result = agent.answer(
        message_text,
        history=history,
        rag_context=rag_context,
        greeting_required=not any(item["role"] == "assistant" for item in history),
        verbose=False,
    )
    agent_decision = dict(result.get("decision") or {})
    routing = resolve_harness_routing(
        message_text, history,
        decision=agent_decision,
        handoff=result.get("handoff"),
        knowledge_retrieval=retrieval,
        dynamic_requirements=dynamic_checks,
    )
    reply_before_routing = str(result.get("reply") or "")
    reply_after_routing = align_reply_with_routing(
        reply_before_routing, routing, dynamic_requirements=dynamic_checks,
    )
    return {
        "message": message_text,
        "reply_before_routing": reply_before_routing,
        "reply_after_routing": reply_after_routing,
        "routing_overrode_reply": reply_after_routing.strip() != reply_before_routing.strip(),
        "agent_decision": agent_decision,
        "effective_decision": routing["decision"],
        "effective_handoff": routing["handoff"],
        "routing_source": routing["source"],
        "sale_candidate": result.get("sale_candidate"),
        "commercial_trace": result.get("commercial_trace") or {},
        "found_live_candidates": bool(live_candidate_context),
        "missing_information": [
            requirement.missing_arguments
            for requirement in (dynamic_checks or ())
            if requirement.status == "missing_arguments"
        ],
        "missing_information_flat": sorted({
            argument
            for requirement in (dynamic_checks or ())
            if requirement.status == "missing_arguments"
            for argument in requirement.missing_arguments
        }),
        "model_calls": result.get("model_calls"),
        "usage": result.get("usage") or {},
    }


def assess_case(case: Dict[str, Any], turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    expect = case.get("expect") or {}
    last = turns[-1]
    failures = []

    action_not_in = set(expect.get("action_not_in") or ())
    if last["effective_decision"].get("action") in action_not_in:
        failures.append(
            "action={} está en action_not_in={}".format(
                last["effective_decision"].get("action"), sorted(action_not_in)
            )
        )

    action_in = expect.get("action_in")
    if action_in and last["effective_decision"].get("action") not in set(action_in):
        failures.append(
            "action={} no está en action_in esperado={}".format(
                last["effective_decision"].get("action"), action_in
            )
        )

    forbid_missing = set(expect.get("forbid_missing_information") or ())
    present_forbidden = forbid_missing & set(last["missing_information_flat"])
    if present_forbidden:
        failures.append(
            "missing_information incluye {} (prohibido para este caso)".format(
                sorted(present_forbidden)
            )
        )

    require_missing = expect.get("require_missing_information")
    if require_missing is not None:
        missing_required = set(require_missing) - set(last["missing_information_flat"])
        if missing_required:
            failures.append(
                "missing_information no incluye lo requerido: {}".format(sorted(missing_required))
            )

    if expect.get("sale_candidate_none") and last["sale_candidate"]:
        failures.append("sale_candidate no es None: {}".format(last["sale_candidate"]))

    if expect.get("require_live_candidates") and not last["found_live_candidates"]:
        failures.append("_live_candidate_context no encontró candidatas verificadas")

    if expect.get("require_handoff") and not last["effective_handoff"]:
        failures.append("se esperaba handoff y no ocurrió")

    # The generic live-check gate can legitimately still *exist* as a
    # DynamicRequirement (infer_dynamic_requirements is untouched by D1.1) —
    # what D1.1 changes is whether align_reply_with_routing acts on it. So
    # the correct signal for "did the grounded-discovery bypass work" is the
    # delivered reply, not the raw presence of "sku" in missing_information.
    if expect.get("forbid_reply_equals") and last["reply_after_routing"].strip() == expect["forbid_reply_equals"]:
        failures.append("la respuesta final quedó reemplazada por el template genérico de SKU")

    return {
        "id": case["id"],
        "category": case["category"],
        "pass": not failures,
        "failures": failures,
        "final_action": last["effective_decision"].get("action"),
        "final_reason": last["effective_decision"].get("reason"),
        "response_mode": last["agent_decision"].get("response_mode"),
        "match_type": last["agent_decision"].get("match_type"),
        "missing_information": last["missing_information_flat"],
        "routing_overrode_reply": last["routing_overrode_reply"],
        "found_live_candidates": last["found_live_candidates"],
        "model_calls": last.get("model_calls"),
        "reply_before_routing": last["reply_before_routing"],
        "reply_after_routing": last["reply_after_routing"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Ejecuta llamadas reales a DeepSeek/Tiendanube/Supabase (solo lectura).")
    parser.add_argument("--allow-large-batch", action="store_true", help="Permite correr más de 10 casos live.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", type=str, default=None, help="IDs separados por coma.")
    parser.add_argument("--repeat", type=int, default=1, help="Repite cada caso N veces (para medir no-determinismo).")
    args = parser.parse_args()

    cases = load_cases()
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if args.repeat > 1:
        cases = cases * args.repeat

    if not args.live:
        print("Modo seguro: {} casos listos. Usá --live para correrlos.".format(len(cases)))
        return
    if len(cases) > 10 and not args.allow_large_batch:
        parser.error("Más de 10 casos live requiere --allow-large-batch.")

    import app as production_app  # noqa: E402  (side-effect free: solo registra rutas)
    # Producción corre con Knowledge RAG en Supabase (confirmado por los logs
    # reales: el bug real mostraba governing_topic=lashes_guidance). El .env
    # local no lo tiene seteado, así que sin esto el shadow correría con
    # Knowledge RAG apagado y nunca ejecutaría infer_dynamic_requirements —
    # exactamente el mecanismo que estamos auditando.
    production_app.KNOWLEDGE_RAG_ENABLED = True
    production_app.KNOWLEDGE_RAG_SOURCE = "supabase"
    assert_shadow_is_read_only()

    results = []
    for case in cases:
        history: List[Dict[str, str]] = []
        turns = []
        print("\n=== {} [{}] ===".format(case["id"], case["category"]))
        for message_text in case["messages"]:
            turn = run_turn(production_app, message_text, history)
            turns.append(turn)
            history.extend([
                {"role": "user", "content": message_text},
                {"role": "assistant", "content": turn["reply_after_routing"]},
            ])
            print("  > {}".format(message_text.replace("\n", " / ")))
            print(
                "    agent: response_mode={} match_type={} | efectivo: action={} reason={} "
                "| missing={} | live_candidates={} | routing_overrode={} | model_calls={}".format(
                    turn["agent_decision"].get("response_mode"),
                    turn["agent_decision"].get("match_type"),
                    turn["effective_decision"].get("action"),
                    turn["effective_decision"].get("reason"),
                    turn["missing_information_flat"],
                    turn["found_live_candidates"],
                    turn["routing_overrode_reply"],
                    turn["model_calls"],
                )
            )
        assessment = assess_case(case, turns)
        results.append(assessment)
        status = "PASS" if assessment["pass"] else "FAIL"
        print("  Reply final: {}".format(assessment["reply_after_routing"].replace("\n", " ")[:200]))
        print("  [{}] {}".format(status, "; ".join(assessment["failures"]) or "sin fallas"))

    passed = sum(1 for r in results if r["pass"])
    print("\n=== RESUMEN ===")
    print("{}/{} casos OK".format(passed, len(results)))
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
    for category, rows in by_category.items():
        ok = sum(1 for r in rows if r["pass"])
        print("  {}: {}/{}".format(category, ok, len(rows)))
    print("\nCasos con clarify_product efectivo:")
    for r in results:
        if r["final_action"] == "clarify_product":
            print("  - {} (categoría {})".format(r["id"], r["category"]))
    print("\nCasos donde el routing pisó la respuesta del agente (routing_overrode_reply=True):")
    for r in results:
        if r["routing_overrode_reply"]:
            print("  - {} (categoría {}, missing={})".format(r["id"], r["category"], r["missing_information"]))
    print("\nCasos donde se exigió SKU (missing_information contiene 'sku'):")
    for r in results:
        if "sku" in r["missing_information"]:
            print("  - {} (categoría {})".format(r["id"], r["category"]))

    output_path = PROJECT / "evals" / "results" / "product_discovery_d1_baseline.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nDetalle completo guardado en {}".format(output_path))


if __name__ == "__main__":
    main()
