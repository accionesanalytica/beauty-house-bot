"""Read-only, fail-open shadow runtime for Fred v2.

Production v1 owns delivery and state.  This module receives immutable copies
only after a v1 response was delivered, runs in bounded background executors,
and writes solely to the isolated shadow observation table.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from operations_store import record_v2_shadow_observation
from v2_agent import FredV2Agent, make_model_call
from v2_tools import V2ToolAdapters


DEFAULT_SHADOW_TIMEOUT_SECONDS = 12.0
MAX_PENDING_SHADOW_TURNS = 8
ALLOWED_SHADOW_TOOLS = {
    "search_knowledge", "get_order", "get_product", "handoff_to_isa",
}
ALLOWED_SHADOW_LOG_FIELDS = {
    "proposed_reply", "tools", "tool_results", "latency_ms", "decision",
    "model_calls", "tokens", "errors",
}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def shadow_enabled() -> bool:
    return os.getenv("FRED_V2_SHADOW_ENABLED", "false").strip().lower() == "true"


def shadow_timeout_seconds() -> float:
    try:
        return max(1.0, min(60.0, float(
            os.getenv("FRED_V2_SHADOW_TIMEOUT_SECONDS", DEFAULT_SHADOW_TIMEOUT_SECONDS)
        )))
    except (TypeError, ValueError):
        return DEFAULT_SHADOW_TIMEOUT_SECONDS


def _hash_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _redact_text(value: Any) -> str:
    text = str(value or "")[:4000]
    return _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", text))


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key)[:80]: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


def _simulated_handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "simulated_success",
        "would_handoff": True,
        "side_effect_executed": False,
        "reason": payload["reason"],
        "summary": payload["summary"],
    }


class ShadowReadOnlyTools(V2ToolAdapters):
    """Closed tool adapter that rejects every mutation signal."""

    def __init__(self, **read_adapters: Any) -> None:
        if "handoff" in read_adapters:
            raise ValueError("Shadow no permite inyectar un handoff con side effects")
        super().__init__(handoff=_simulated_handoff, **read_adapters)

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in ALLOWED_SHADOW_TOOLS:
            raise ValueError("Operación no read-only rechazada: {}".format(name))
        result = super().call(name, arguments)
        if result.get("side_effect_executed") is True:
            raise RuntimeError("Una tool shadow intentó ejecutar side effects")
        return result


def propose_shadow_turn(
    message: str,
    *,
    history: Optional[List[Dict[str, Any]]] = None,
    agent: Optional[FredV2Agent] = None,
) -> Dict[str, Any]:
    shadow_agent = agent or FredV2Agent(
        model_call=make_model_call(timeout_seconds=shadow_timeout_seconds()),
        tools=ShadowReadOnlyTools(),
    )
    result = shadow_agent.answer(message, history=history)
    record = {
        "proposed_reply": result.get("reply", ""),
        "tools": result.get("tool_calls") or [],
        "tool_results": result.get("tool_results") or [],
        "latency_ms": result.get("latency_ms", 0),
        "decision": result.get("decision") or {},
        "model_calls": result.get("model_calls", 0),
        "tokens": result.get("usage") or {},
        "errors": result.get("errors") or [],
    }
    return {key: record[key] for key in ALLOWED_SHADOW_LOG_FIELDS}


@dataclass(frozen=True)
class ShadowTurn:
    correlation_id: str
    conversation_id: int
    generation: int
    customer_phone: str
    source_message_id_hash: str
    message: str
    history: tuple


current_shadow_turn: ContextVar[Optional[ShadowTurn]] = ContextVar(
    "current_shadow_turn", default=None,
)


def clear_shadow_turn() -> None:
    current_shadow_turn.set(None)


def begin_shadow_turn(
    *,
    conversation_id: int,
    generation: int,
    customer_phone: str,
    source_message_id: str,
    message: str,
    history: List[Dict[str, Any]],
) -> bool:
    """Capture immutable inputs only when the explicit flag is enabled."""
    clear_shadow_turn()
    if not shadow_enabled():
        return False
    current_shadow_turn.set(ShadowTurn(
        correlation_id=str(uuid.uuid4()),
        conversation_id=int(conversation_id),
        generation=max(0, int(generation or 0)),
        customer_phone=str(customer_phone or ""),
        source_message_id_hash=_hash_text(source_message_id),
        message=str(message or ""),
        history=tuple(dict(item) for item in (history or [])[-8:]),
    ))
    return True


_supervisor_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fred-v2-shadow")
_agent_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fred-v2-agent")
_pending_slots = threading.BoundedSemaphore(MAX_PENDING_SHADOW_TURNS)


def _handoff_reason(tool_calls: List[Dict[str, Any]]) -> str:
    for call in reversed(tool_calls):
        if call.get("name") == "handoff_to_isa":
            return str((call.get("arguments") or {}).get("reason") or "")[:80]
    return ""


def _safe_tool_results(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only tool evidence already designed for customer-safe evaluation."""
    return [{
        "name": str(row.get("name") or "")[:80],
        "result": _redact_value(row.get("result") or {}),
    } for row in rows]


def _observation(turn: ShadowTurn, v1_response: str, result: Dict[str, Any]) -> Dict[str, Any]:
    errors = result.get("errors") or []
    usage = result.get("tokens") or {}
    v2_response = result.get("proposed_reply") or ""
    return {
        "correlation_id": turn.correlation_id,
        "source_message_id_hash": turn.source_message_id_hash,
        "conversation_id": turn.conversation_id,
        "generation": turn.generation,
        "input_text_redacted": _redact_text(turn.message),
        "v1_response_redacted": _redact_text(v1_response),
        "v2_response_redacted": _redact_text(v2_response),
        "v1_response_hash": _hash_text(v1_response),
        "v2_response_hash": _hash_text(v2_response),
        "v2_tool_calls": _redact_value(result.get("tools") or []),
        "v2_tool_results": _safe_tool_results(result.get("tool_results") or []),
        "v2_llm_calls": result.get("model_calls") or 0,
        "v2_prompt_tokens": usage.get("prompt_tokens") or 0,
        "v2_completion_tokens": usage.get("completion_tokens") or 0,
        "v2_latency_ms": result.get("latency_ms") or 0,
        "v2_handoff_reason": _handoff_reason(result.get("tools") or []),
        "error_type": str(errors[0])[:120] if errors else "",
        "side_effects": False,
    }


def _log_shadow(observation: Dict[str, Any]) -> None:
    print(
        "[FredShadow] correlation_id={} conversation_id={} generation={} "
        "v1_response_hash={} v2_response_hash={} v2_tools={} v2_llm_calls={} "
        "v2_tokens={} v2_latency_ms={} v2_handoff_reason={} v2_error={} side_effects=false".format(
            observation["correlation_id"], observation["conversation_id"],
            observation["generation"], observation["v1_response_hash"],
            observation["v2_response_hash"],
            ",".join(call.get("name", "") for call in observation["v2_tool_calls"]) or "none",
            observation["v2_llm_calls"],
            observation["v2_prompt_tokens"] + observation["v2_completion_tokens"],
            round(float(observation["v2_latency_ms"] or 0)),
            observation["v2_handoff_reason"] or "none",
            observation["error_type"] or "none",
        )
    )


def _run_and_record(
    turn: ShadowTurn,
    v1_response: str,
    *,
    proposer: Callable[..., Dict[str, Any]] = propose_shadow_turn,
    recorder: Callable[[Dict[str, Any]], bool] = record_v2_shadow_observation,
) -> None:
    deadline = shadow_timeout_seconds()
    future = _agent_executor.submit(
        proposer, turn.message, history=[dict(item) for item in turn.history],
    )
    try:
        result = future.result(timeout=deadline)
    except FutureTimeout:
        future.cancel()
        result = {
            "proposed_reply": "", "tools": [], "tool_results": [], "latency_ms": deadline * 1000,
            "decision": {}, "model_calls": 0, "tokens": {}, "errors": ["shadow_timeout"],
        }
    except Exception as error:  # noqa: BLE001
        result = {
            "proposed_reply": "", "tools": [], "tool_results": [], "latency_ms": 0,
            "decision": {}, "model_calls": 0, "tokens": {},
            "errors": ["shadow_exception:{}".format(type(error).__name__)],
        }
    observation = _observation(turn, v1_response, result)
    try:
        recorder(observation)
    except Exception as error:  # noqa: BLE001
        observation["error_type"] = observation["error_type"] or "log_error:{}".format(type(error).__name__)
    _log_shadow(observation)


def _release_slot(_future: Any) -> None:
    _pending_slots.release()


def observe_v1_delivery(phone_number: str, response: str) -> bool:
    """Submit shadow after v1 delivery; every failure is swallowed (fail-open)."""
    turn = current_shadow_turn.get()
    if not turn or str(phone_number or "") != turn.customer_phone:
        return False
    clear_shadow_turn()
    if not _pending_slots.acquire(blocking=False):
        print("[FredShadow] dropped=queue_full side_effects=false")
        return False
    try:
        future = _supervisor_executor.submit(_run_and_record, turn, str(response or ""))
        future.add_done_callback(_release_slot)
        return True
    except Exception as error:  # noqa: BLE001
        _pending_slots.release()
        print("[FredShadow] dispatch_error={} side_effects=false".format(type(error).__name__))
        return False
