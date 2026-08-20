"""Side-effect-free A/B harness for running the same turns through v1 and v2."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional


Runner = Callable[[str, List[Dict[str, Any]]], Dict[str, Any]]
HallucinationCheck = Callable[[str, Dict[str, Any]], List[str]]


def evidence_hallucination_check(message: str, result: Dict[str, Any]) -> List[str]:
    """Flag customer-visible URLs absent from tool evidence.

    Broader factual correctness still needs the human/eval rubric; this check is
    intentionally narrow so the harness never claims that silence means truth.
    """
    del message
    evidence = str(result.get("tool_results") or [])
    findings = []
    for token in str(result.get("reply") or "").split():
        url = token.strip(".,;!?()[]")
        if url.startswith(("http://", "https://")) and url not in evidence:
            findings.append("unverified_url:{}".format(url))
    return findings


def _run_one(
    runner: Runner,
    message: str,
    history: List[Dict[str, Any]],
    checker: Optional[HallucinationCheck],
) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        result = runner(message, history)
        errors = list(result.get("errors") or [])
    except Exception as error:  # noqa: BLE001
        result = {"reply": "", "tool_calls": [], "model_calls": 0}
        errors = ["{}: {}".format(type(error).__name__, error)]
    latency_ms = result.get("latency_ms")
    if latency_ms is None:
        latency_ms = round((time.monotonic() - started) * 1000, 2)
    hallucinations = checker(message, result) if checker else []
    return {
        "reply": result.get("reply", ""),
        "tools": [item.get("name") for item in result.get("tool_calls") or []],
        "tool_calls": result.get("tool_calls") or [],
        "tool_results": result.get("tool_results") or [],
        "model_calls": int(result.get("model_calls") or 0),
        "latency_ms": latency_ms,
        "errors": errors,
        "hallucinations": hallucinations,
    }


def compare_turns(
    messages: Iterable[str],
    *,
    v1_runner: Runner,
    v2_runner: Runner,
    hallucination_check: Optional[HallucinationCheck] = evidence_hallucination_check,
) -> List[Dict[str, Any]]:
    """Compare runners with identical evolving customer/assistant context.

    Histories stay separate because each version must see its own prior answer.
    The harness only records returned data; whether a runner uses external
    services is the caller's explicit choice.
    """
    v1_history: List[Dict[str, Any]] = []
    v2_history: List[Dict[str, Any]] = []
    comparisons = []
    for message in messages:
        v1 = _run_one(v1_runner, message, v1_history, hallucination_check)
        v2 = _run_one(v2_runner, message, v2_history, hallucination_check)
        comparisons.append({"message": message, "v1": v1, "v2": v2})
        v1_history.extend((
            {"role": "user", "content": message},
            {"role": "assistant", "content": v1["reply"]},
        ))
        v2_history.extend((
            {"role": "user", "content": message},
            {"role": "assistant", "content": v2["reply"]},
        ))
    return comparisons
