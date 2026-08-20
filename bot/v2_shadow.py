"""Safe shadow envelope for a future v1-primary/v2-observer execution.

This module is intentionally not imported by ``app.py``.  It accepts already
loaded history, calls only the supplied v2 agent, and returns a log record.  The
default v2 handoff adapter is preview-only, so shadow cannot notify Isa, mutate
conversation state, send WhatsApp, or create checkout.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from v2_agent import FredV2Agent


ALLOWED_SHADOW_LOG_FIELDS = {
    "proposed_reply", "tools", "latency_ms", "decision", "model_calls",
    "tokens", "errors",
}


def propose_shadow_turn(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    agent: Optional[FredV2Agent] = None,
) -> Dict[str, Any]:
    result = (agent or FredV2Agent()).answer(message, history=history)
    record = {
        "proposed_reply": result.get("reply", ""),
        "tools": result.get("tool_calls") or [],
        "latency_ms": result.get("latency_ms", 0),
        "decision": result.get("decision") or {},
        "model_calls": result.get("model_calls", 0),
        "tokens": result.get("usage") or {},
        "errors": result.get("errors") or [],
    }
    return {key: record[key] for key in ALLOWED_SHADOW_LOG_FIELDS}
