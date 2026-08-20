"""Aggregate exported Fred v2 shadow observations without production writes.

Input is a JSON array or JSONL export of ``fred_v2_shadow_observations``.
Optional offline rubric fields (expected_tools, expected_handoff_reason,
expected_order_number, v1_outcome) improve semantic scoring; unannotated safe
turns remain REVIEW instead of being guessed as correct.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


def load_rows(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def percentile(values: List[float], point: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(float(ordered[max(0, math.ceil(len(ordered) * point) - 1)]), 2)


def _tool_names(row: Dict[str, Any]) -> List[str]:
    return [str(call.get("name") or "") for call in row.get("v2_tool_calls") or []]


def hallucination_flags(row: Dict[str, Any]) -> List[str]:
    reply = str(row.get("v2_response_redacted") or "").lower()
    evidence = json.dumps(row.get("v2_tool_results") or [], ensure_ascii=False).lower()
    tools = _tool_names(row)
    flags = []
    if "checkout" in reply or any("checkout" in name.lower() for name in tools):
        flags.append("checkout")
    for price in re.findall(r"\$\s*([0-9][0-9.,]*)", reply):
        digits = "".join(char for char in price if char.isdigit())
        if digits and digits not in "".join(char for char in evidence if char.isdigit()):
            flags.append("unsupported_price")
    if any(term in reply for term in ("hay stock", "tenemos stock", "está disponible", "sin stock")):
        if "get_product" not in tools:
            flags.append("unsupported_stock")
    if any(term in reply for term in ("despachado", "entregado", "empaquetado", "en preparación")):
        if "get_order" not in tools:
            flags.append("unsupported_order")
    return list(dict.fromkeys(flags))


def evaluate(row: Dict[str, Any]) -> Dict[str, Any]:
    tools = _tool_names(row)
    flags = hallucination_flags(row)
    blockers = []
    if row.get("side_effects") is not False:
        blockers.append("side_effect")
    if row.get("error_type"):
        blockers.append("shadow_error")
    if flags:
        blockers.append("hallucination")

    expected_tools = row.get("expected_tools")
    if expected_tools is not None and set(tools) != set(expected_tools):
        blockers.append("wrong_tool")
    expected_handoff = row.get("expected_handoff_reason")
    handoff_accurate = None if expected_handoff is None else (
        str(row.get("v2_handoff_reason") or "") == str(expected_handoff)
    )
    if handoff_accurate is False:
        blockers.append("wrong_handoff")
    expected_order = row.get("expected_order_number")
    order_calls = [
        str((call.get("arguments") or {}).get("order_number") or "")
        for call in row.get("v2_tool_calls") or [] if call.get("name") == "get_order"
    ]
    order_accurate = None if expected_order is None else str(expected_order) in order_calls
    if order_accurate is False:
        blockers.append("wrong_order")
    stale_context = bool(row.get("stale_context_failure"))
    if stale_context:
        blockers.append("stale_context")

    blockers = list(dict.fromkeys(blockers))
    if blockers:
        status = "FAIL"
    elif row.get("rubric_outcome") in {"PASS", "REVIEW", "FAIL"}:
        status = row["rubric_outcome"]
    else:
        status = "REVIEW"
    return {
        "status": status, "blockers": blockers, "hallucination_flags": flags,
        "handoff_accurate": handoff_accurate, "order_accurate": order_accurate,
        "stale_context": stale_context,
    }


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    evaluated = [{**row, "evaluation": evaluate(row)} for row in rows]
    statuses = Counter(row["evaluation"]["status"] for row in evaluated)
    latencies = [float(row.get("v2_latency_ms") or 0) for row in evaluated]
    score = {"PASS": 2, "REVIEW": 1, "FAIL": 0}
    v1_wins = v2_wins = ties = 0
    for row in evaluated:
        v1 = row.get("v1_outcome") if row.get("v1_outcome") in score else "REVIEW"
        v2 = row["evaluation"]["status"]
        if score[v1] > score[v2]:
            v1_wins += 1
        elif score[v2] > score[v1]:
            v2_wins += 1
        else:
            ties += 1
    total = len(evaluated)
    handoff_rows = [r for r in evaluated if r["evaluation"]["handoff_accurate"] is not None]
    order_rows = [r for r in evaluated if r["evaluation"]["order_accurate"] is not None]
    return {
        "total": total,
        "pass": statuses["PASS"], "review": statuses["REVIEW"], "fail": statuses["FAIL"],
        "blockers": dict(Counter(
            blocker for row in evaluated for blocker in row["evaluation"]["blockers"]
        )),
        "win_rate": {
            "v1": round(v1_wins / total, 4) if total else 0,
            "v2": round(v2_wins / total, 4) if total else 0,
            "ties": round(ties / total, 4) if total else 0,
        },
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "tool_calls_avg": round(statistics.mean(len(_tool_names(row)) for row in evaluated), 3) if total else 0,
        "llm_calls_avg": round(statistics.mean(float(row.get("v2_llm_calls") or 0) for row in evaluated), 3) if total else 0,
        "tokens_avg": round(statistics.mean(
            float(row.get("v2_prompt_tokens") or 0) + float(row.get("v2_completion_tokens") or 0)
            for row in evaluated
        ), 2) if total else 0,
        "hallucination_flags": dict(Counter(
            flag for row in evaluated for flag in row["evaluation"]["hallucination_flags"]
        )),
        "handoff_accuracy": (
            round(sum(r["evaluation"]["handoff_accurate"] is True for r in handoff_rows) / len(handoff_rows), 4)
            if handoff_rows else None
        ),
        "order_accuracy": (
            round(sum(r["evaluation"]["order_accurate"] is True for r in order_rows) / len(order_rows), 4)
            if order_rows else None
        ),
        "stale_context_failures": sum(row["evaluation"]["stale_context"] for row in evaluated),
        "rows": evaluated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = aggregate(load_rows(args.input))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
