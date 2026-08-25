"""Production runtime for the exclusive Fred v2 customer-response path."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from operations_store import (
    acknowledge_v2_handoff,
    get_v2_handoff_by_correlation,
    list_pending_v2_handoffs,
    mark_v2_handoff_delivery,
    record_v2_events,
    record_v2_handoff,
)
from v2_agent import FredV2Agent
from v2_live_adapters import (
    ISA_PUBLIC_DISPLAY,
    REASON_LABELS,
    build_isa_wa_url,
    live_handoff_adapter,
)
from v2_tools import V2ToolAdapters


_ORDER_HANDOFF_REASONS = {
    "cancel_order", "modify_order", "return_order", "sensitive_order_action",
}
_PRODUCT_HANDOFF_REASONS = {"custom_order", "purchase_intent", "product_advice"}
_TOOL_NAMES = {"search_knowledge", "get_order", "get_product", "handoff_to_isa"}

_CLEAR_PURCHASE_RE = re.compile(
    r"^\s*(?:quiero|quisiera|me\s+gustar[ií]a)\s+comprar\s+(.+?)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_SENSITIVE_PURCHASE_WORDS = re.compile(
    r"\b(?:cancel|devolv|modific|cambi|pedido\s+#|hablar\s+con|persona|humano|"
    r"dni|direcci[oó]n|domicilio|vivo\s+en|ll[aá]mame|escribime)\w*\b",
    re.IGNORECASE,
)


def _clear_purchase_handoff(message: str) -> Optional[Dict[str, str]]:
    """Recognize only an explicit, self-contained purchase; ambiguity stays with the model."""
    compact = " ".join(str(message or "").split())
    match = _CLEAR_PURCHASE_RE.fullmatch(compact)
    if not match or "?" in compact or _SENSITIVE_PURCHASE_WORDS.search(compact):
        return None
    product = match.group(1).strip(" .,!;:")
    if not product or len(product) > 100:
        return None
    return {
        "operation": "create",
        "reason": "purchase_intent",
        "reason_label": "compra de {}".format(product),
        "context_summary": (
            "quiero comprar {} y necesito ayuda para coordinar la compra."
        ).format(product),
    }


def _direct_handoff_result(
    handoff: Callable[[Dict[str, Any]], Dict[str, Any]], payload: Dict[str, str],
) -> Dict[str, Any]:
    tool_result = handoff(payload)
    return {
        "reply": tool_result["customer_safe_reply"],
        "tool_calls": [{"name": "handoff_to_isa", "arguments": {
            "operation": "create", "reason": "purchase_intent",
        }}],
        "tool_results": [{"name": "handoff_to_isa", "result": tool_result}],
        "model_calls": 0,
        "latency_ms": 0,
        "errors": [],
        "usage": {},
        "decision": {"action": "handoff_to_isa", "reason": "purchase_intent"},
        "fast_path": "clear_purchase_handoff",
    }


def correlation_id_for(source_message_id: str, conversation_id: int, generation: int) -> str:
    source = str(source_message_id or "").strip()
    if not source:
        source = uuid.uuid4().hex
    seed = "{}:{}:{}".format(int(conversation_id), max(0, int(generation or 0)), source)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _error_label(prefix: str, error: BaseException) -> str:
    return "{}:{}".format(prefix, type(error).__name__)[:120]


def _active_handoff_context(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Expose only canonical state needed for semantic follow-up decisions."""
    state = []
    for item in pending:
        state.append({
            "handoff_id": int(item["id"]),
            "reason": item["reason"],
            "order_number": item.get("order_number") or None,
            "status": "pending",
        })
    return state


def _topic_for(tool_calls: List[Dict[str, Any]]) -> str:
    topics = []
    for call in tool_calls:
        name = call.get("name")
        arguments = call.get("arguments") or {}
        if name == "get_order":
            topics.append("order")
        elif name == "get_product":
            topics.append("product")
        elif name == "search_knowledge":
            topics.append("knowledge")
        elif name == "handoff_to_isa":
            reason = arguments.get("reason")
            if reason in _ORDER_HANDOFF_REASONS:
                topics.append("order")
            elif reason in _PRODUCT_HANDOFF_REASONS:
                topics.append("product")
            elif reason == "operational_detail_unverified":
                topics.append("knowledge")
            else:
                topics.append("handoff")
    for preferred in ("order", "product", "knowledge", "handoff"):
        if preferred in topics:
            return preferred
    return "general"


def _safe_tool_name(value: Any) -> str:
    name = str(value or "")
    return name if name in _TOOL_NAMES else "unknown"


def _outbox_action_for_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the single bounded state action stored beside the response."""
    for row in result.get("tool_results") or []:
        if row.get("name") != "handoff_to_isa":
            continue
        tool_result = row.get("result") or {}
        operation = str(tool_result.get("operation") or "create")
        reason = str(tool_result.get("reason") or "")
        try:
            handoff_id = int(tool_result.get("handoff_id") or 0)
        except (TypeError, ValueError):
            handoff_id = 0
        if operation in {"create", "repeat", "resolve"} and handoff_id and reason in REASON_LABELS:
            return {
                "operation": operation,
                "handoff_id": handoff_id,
                "reason": reason,
            }
    return {}


def _result_from_outbox(
    row: Dict[str, Any], *, errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Re-use the canonical stored response without running the model/tools."""
    operation = str(row.get("handoff_operation") or "")
    reason = str(row.get("handoff_reason") or "")
    try:
        handoff_id = int(row.get("handoff_id") or 0)
    except (TypeError, ValueError):
        handoff_id = 0
    action = {}
    if operation in {"create", "repeat", "resolve"} and handoff_id and reason in REASON_LABELS:
        action = {
            "operation": operation,
            "handoff_id": handoff_id,
            "reason": reason,
            "action_applied": row.get("status") == "sent",
        }
    return {
        "reply": str(row.get("body") or ""),
        "tool_calls": [],
        "tool_results": [],
        "model_calls": 0,
        "latency_ms": 0,
        "errors": list(errors or []),
        "usage": {},
        "decision": {"action": "outbox_replay_suppressed"},
        "outbox_action": action,
        "outbox_replay": True,
    }


def _result_from_orphan_handoff(
    row: Dict[str, Any], *, conversation_id: int, correlation_id: str,
) -> Dict[str, Any]:
    """Rebuild deterministic copy after a crash between handoff and outbox."""
    reason = str(row.get("reason") or "")
    if reason not in REASON_LABELS:
        raise ValueError("orphan handoff reason invalid")
    handoff = live_handoff_adapter(
        conversation_id=conversation_id,
        correlation_id=correlation_id,
        pending_handoffs=(),
        create_handoff=lambda **_kwargs: dict(row),
    )
    tool_result = handoff({
        "operation": "create",
        "reason": reason,
        "order_number": row.get("order_number") or "",
    })
    return {
        "reply": tool_result["customer_safe_reply"],
        "tool_calls": [{
            "name": "handoff_to_isa",
            "arguments": {"operation": "create", "reason": reason},
        }],
        "tool_results": [{"name": "handoff_to_isa", "result": tool_result}],
        "model_calls": 0,
        "latency_ms": 0,
        "errors": ["handoff_outbox_recovered"],
        "usage": {},
        "decision": {"action": "handoff_to_isa", "reason": reason},
    }


def _apply_finish_result(result: Dict[str, Any], finished: Dict[str, Any]) -> None:
    """Reflect the atomic outbox/handoff commit in result telemetry."""
    operation = str(finished.get("handoff_operation") or "")
    try:
        handoff_id = int(finished.get("handoff_id") or 0)
    except (TypeError, ValueError):
        handoff_id = 0
    reason = str(finished.get("handoff_reason") or "")
    applied = bool(finished.get("handoff_action_applied"))
    if operation in {"create", "repeat", "resolve"} and handoff_id and reason in REASON_LABELS:
        result["outbox_action"] = {
            "operation": operation,
            "handoff_id": handoff_id,
            "reason": reason,
            "action_applied": applied,
        }
    if operation == "resolve":
        for row in result.get("tool_results") or []:
            tool_result = row.get("result") or {}
            if (
                row.get("name") == "handoff_to_isa"
                and tool_result.get("operation") == "resolve"
                and int(tool_result.get("handoff_id") or 0) == handoff_id
            ):
                tool_result["resolution_committed"] = applied
                tool_result["side_effect_executed"] = applied
    if operation in {"create", "resolve"} and finished.get("status") == "sent" and not applied:
        result.setdefault("errors", []).append("handoff_state_commit_missing")


def _fallback_result(
    *, handoff: Callable[[Dict[str, Any]], Dict[str, Any]], error: BaseException,
) -> Dict[str, Any]:
    safe_error = str(
        getattr(error, "error_code", "") or _error_label("agent", error)
    )[:120]
    prior_calls = list(getattr(error, "tool_calls", []) or [])
    prior_results = list(getattr(error, "tool_results", []) or [])
    model_calls = max(0, int(getattr(error, "model_calls", 0) or 0))
    usage = dict(getattr(error, "usage", {}) or {})
    fallback_reason = str(
        getattr(error, "fallback_reason", "") or "unable_to_verify"
    )
    if fallback_reason not in REASON_LABELS:
        fallback_reason = "unable_to_verify"
    fallback_summary = str(
        getattr(error, "fallback_summary", "")
        or "Fred tuvo un problema técnico y no pudo confirmar la consulta."
    )[:240]
    try:
        handoff_result = handoff({
            "operation": "create",
            "reason": fallback_reason,
            "summary": fallback_summary,
        })
        reply = handoff_result["customer_safe_reply"]
        return {
            "reply": reply,
            "tool_calls": prior_calls + [{
                "name": "handoff_to_isa",
                "arguments": {
                    "operation": "create", "reason": fallback_reason,
                },
            }],
            "tool_results": prior_results + [{
                "name": "handoff_to_isa", "result": handoff_result,
            }],
            "model_calls": model_calls,
            "latency_ms": 0,
            "errors": [safe_error],
            "usage": usage,
            "decision": {"action": "handoff_to_isa", "reason": fallback_reason},
        }
    except Exception as fallback_error:  # noqa: BLE001
        result = _unpersisted_contact_result(
            safe_error, _error_label("fallback", fallback_error),
        )
        result["tool_calls"] = prior_calls
        result["tool_results"] = prior_results
        result["model_calls"] = model_calls
        result["usage"] = usage
        return result


def _unpersisted_contact_result(*error_labels: str) -> Dict[str, Any]:
    """Fail closed with honest customer-owned contact copy and no model prose."""
    errors = [str(label)[:120] for label in error_labels if label]
    lead = (
        "Ahora no puedo consultar esto con seguridad. "
        "Este tema lo tiene que revisar Isa."
    )
    try:
        url = build_isa_wa_url(
            reason_label=REASON_LABELS["unable_to_verify"],
            context_summary="Fred tuvo un problema técnico y no pudo confirmar la consulta.",
        )
        reply = "{}\n\nWhatsApp de Isa: {}\n{}".format(
            lead, ISA_PUBLIC_DISPLAY, url,
        )
    except Exception as url_error:  # noqa: BLE001
        errors.append(_error_label("contact_link", url_error))
        reply = "{}\n\nWhatsApp de Isa: {}".format(lead, ISA_PUBLIC_DISPLAY)
    return {
        "reply": reply,
        "tool_calls": [],
        "tool_results": [],
        "model_calls": 0,
        "latency_ms": 0,
        "errors": errors,
        "usage": {},
        "decision": {"action": "service_fallback", "reason": "v2_error"},
        "contact_link_offered": "https://wa.me/" in reply,
    }


def _prepared_handoffs(result: Dict[str, Any]) -> List[int]:
    ids = []
    for row in result.get("tool_results") or []:
        tool_result = row.get("result") or {}
        if (
            row.get("name") == "handoff_to_isa"
            and tool_result.get("operation") == "create"
            and tool_result.get("handoff_id")
        ):
            ids.append(int(tool_result["handoff_id"]))
    return sorted(set(ids))


def _events_for_result(
    *,
    result: Dict[str, Any],
    correlation_id: str,
    conversation_id: int,
    delivered: bool,
    duration_ms: int,
    delivery_outcome: str = "",
) -> List[Dict[str, Any]]:
    calls = result.get("tool_calls") or []
    tool_results = result.get("tool_results") or []
    usage = result.get("usage") or {}
    topic = _topic_for(calls)
    base = {"correlation_id": correlation_id, "conversation_id": conversation_id}
    events = [{
        **base,
        "event_type": "response",
        "event_index": 0,
        "topic": topic,
        "outcome": delivery_outcome or ("sent" if delivered else "send_failed"),
        "latency_ms": max(0, int(duration_ms)),
        "llm_calls": max(0, int(result.get("model_calls") or 0)),
        "prompt_tokens": max(0, int(usage.get("prompt_tokens") or 0)),
        "completion_tokens": max(0, int(usage.get("completion_tokens") or 0)),
        "preserve_existing": bool(result.get("outbox_replay")),
    }]
    emitted_handoff_ids = set()
    for index, call in enumerate(calls):
        arguments = call.get("arguments") or {}
        events.append({
            **base,
            "event_type": "tool",
            "event_index": index,
            "topic": _topic_for([call]),
            "outcome": "called",
            "tool_name": _safe_tool_name(call.get("name")),
            "handoff_reason": (
                arguments.get("reason") if call.get("name") == "handoff_to_isa" else None
            ),
        })
        tool_result = (
            (tool_results[index].get("result") or {})
            if index < len(tool_results) else {}
        )
        if call.get("name") != "handoff_to_isa":
            continue
        operation = tool_result.get("operation") or arguments.get("operation") or "create"
        reason = tool_result.get("reason") or arguments.get("reason")
        handoff_id = tool_result.get("handoff_id")
        if (
            operation == "create"
            and delivered
            and handoff_id
            and int(handoff_id) not in emitted_handoff_ids
        ):
            emitted_handoff_ids.add(int(handoff_id))
            events.append({
                **base,
                "event_type": "handoff",
                "event_index": index,
                "topic": _topic_for([call]),
                "outcome": "link_sent",
                "handoff_reason": reason,
            })
        elif operation == "resolve" and tool_result.get("resolution_committed"):
            events.append({
                **base,
                "event_type": "handoff_resolved",
                "event_index": index,
                "topic": _topic_for([call]),
                "outcome": "customer_acknowledged",
                "handoff_reason": reason,
            })
    outbox_action = result.get("outbox_action") or {}
    if delivered and result.get("outbox_replay") and outbox_action.get("action_applied"):
        operation = outbox_action.get("operation")
        reason = outbox_action.get("reason")
        if operation == "create":
            events.append({
                **base,
                "event_type": "handoff",
                "event_index": 0,
                "topic": _topic_for([{
                    "name": "handoff_to_isa", "arguments": {"reason": reason},
                }]),
                "outcome": "link_sent",
                "handoff_reason": reason,
                "preserve_existing": bool(result.get("outbox_replay")),
            })
        elif operation == "resolve":
            events.append({
                **base,
                "event_type": "handoff_resolved",
                "event_index": 0,
                "topic": _topic_for([{
                    "name": "handoff_to_isa", "arguments": {"reason": reason},
                }]),
                "outcome": "customer_acknowledged",
                "handoff_reason": reason,
                "preserve_existing": bool(result.get("outbox_replay")),
            })
    if delivered and result.get("contact_link_offered"):
        events.append({
            **base,
            "event_type": "handoff",
            "event_index": 0,
            "topic": "handoff",
            "outcome": "link_sent_unpersisted",
            "handoff_reason": "unable_to_verify",
        })
    for index, error_type in enumerate(result.get("errors") or []):
        events.append({
            **base,
            "event_type": "error",
            "event_index": index + (1000 if result.get("outbox_replay") else 0),
            "topic": topic,
            "outcome": "handled",
            "error_type": str(error_type or "unknown")[:120],
        })
    return events


def run_v2_customer_turn(
    *,
    customer_phone: str,
    conversation_id: int,
    source_message_id: str,
    generation: int,
    message: str,
    history: List[Dict[str, Any]],
    send_message: Callable[[str, str], bool],
    record_message: Optional[Callable[[int, str], None]] = None,
    load_message: Optional[Callable[[int, str], Optional[Dict[str, Any]]]] = None,
    prepare_message: Optional[Callable[..., Dict[str, Any]]] = None,
    finish_message: Optional[Callable[[int, int, str, bool], Dict[str, Any]]] = None,
    delivery_allowed: Optional[Callable[[], bool]] = None,
    agent_factory: Optional[Callable[..., FredV2Agent]] = None,
    platform_error: str = "",
    replay_only: bool = False,
) -> Dict[str, Any]:
    """Answer, deliver, persist, and observe one v2 turn; v1 is never imported."""
    started = time.monotonic()
    correlation_id = correlation_id_for(source_message_id, conversation_id, generation)
    runtime_errors: List[str] = []
    existing_outbox: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    if conversation_id > 0 and load_message is not None:
        try:
            existing_outbox = load_message(conversation_id, correlation_id)
        except Exception as error:  # noqa: BLE001
            existing_outbox = None
            runtime_errors.append(_error_label("response_outbox_load", error))
        if existing_outbox:
            result = _result_from_outbox(existing_outbox, errors=runtime_errors)
        if existing_outbox and existing_outbox.get("status") == "sent":
            if finish_message is not None:
                try:
                    finished = finish_message(
                        conversation_id,
                        int(existing_outbox.get("message_id") or 0),
                        correlation_id,
                        True,
                    )
                    _apply_finish_result(result, finished)
                    if not finished.get("found"):
                        result["errors"].append("response_outbox_reconcile_not_found")
                except Exception as error:  # noqa: BLE001
                    result["errors"].append(
                        _error_label("response_outbox_reconcile", error)
                    )
            duration_ms = round((time.monotonic() - started) * 1000)
            events = _events_for_result(
                result=result, correlation_id=correlation_id,
                conversation_id=conversation_id, delivered=True,
                duration_ms=duration_ms, delivery_outcome="sent",
            )
            try:
                record_v2_events(events)
            except Exception as error:  # noqa: BLE001
                print("[FredV2] analytics_error={}".format(type(error).__name__))
            print(
                "[FredV2] conversation={} delivered=true topic=general tools=0 "
                "llm_calls=0 latency_ms={} replay_suppressed=true".format(
                    conversation_id, duration_ms,
                )
            )
            return {
                **result, "delivered": True, "correlation_id": correlation_id,
                "duration_ms": duration_ms, "events": events,
            }
    if (
        result is None
        and conversation_id > 0
        and load_message is not None
        and not platform_error
    ):
        try:
            orphan_handoff = get_v2_handoff_by_correlation(
                conversation_id, correlation_id,
            )
        except Exception as error:  # noqa: BLE001
            orphan_handoff = None
            runtime_errors.append(_error_label("handoff_recovery_load", error))
        if orphan_handoff and orphan_handoff.get("status") in {"prepared", "delivery_failed"}:
            try:
                result = _result_from_orphan_handoff(
                    orphan_handoff,
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                )
            except Exception as error:  # noqa: BLE001
                result = _unpersisted_contact_result(
                    _error_label("handoff_recovery", error),
                )
    if replay_only and result is None:
        duration_ms = round((time.monotonic() - started) * 1000)
        print(
            "[FredV2] conversation={} duplicate_replay_miss=true latency_ms={}".format(
                conversation_id, duration_ms,
            )
        )
        return {
            "reply": "", "tool_calls": [], "tool_results": [],
            "model_calls": 0, "latency_ms": 0, "errors": runtime_errors,
            "usage": {}, "decision": {"action": "duplicate_replay_miss"},
            "delivered": False, "correlation_id": correlation_id,
            "duration_ms": duration_ms, "events": [],
        }
    pending: List[Dict[str, Any]] = []
    active_handoffs: List[Dict[str, Any]] = []
    if result is None and not platform_error:
        try:
            pending = list_pending_v2_handoffs(conversation_id)
            active_handoffs = _active_handoff_context(pending)
        except Exception as error:  # noqa: BLE001
            runtime_errors.append(_error_label("handoff_state_load", error))

    if result is not None:
        pass
    elif platform_error:
        result = _unpersisted_contact_result(
            "platform:{}".format(str(platform_error or "unknown")[:100]),
        )
    else:
        handoff = live_handoff_adapter(
            conversation_id=conversation_id,
            correlation_id=correlation_id,
            pending_handoffs=pending,
            create_handoff=record_v2_handoff,
        )
        tools = V2ToolAdapters(handoff=handoff)
        factory = agent_factory or FredV2Agent
        try:
            direct_purchase = _clear_purchase_handoff(message)
            if direct_purchase and not active_handoffs:
                result = _direct_handoff_result(handoff, direct_purchase)
            else:
                result = factory(tools=tools).answer(
                    message, history=history, active_handoffs=active_handoffs,
                )
        except Exception as error:  # noqa: BLE001
            result = _fallback_result(handoff=handoff, error=error)

    # ``platform_error`` intentionally bypasses the model and handoff-state
    # writes: a missing history must not become a stateless AI conversation.
    # Delivery and best-effort analytics still follow the same v2-only path.
    if not platform_error and not isinstance(result, dict):
        result = _unpersisted_contact_result("agent:invalid_result")
    elif not isinstance(result, dict):
        result = _unpersisted_contact_result("platform:invalid_result")

    if platform_error:
        runtime_errors = []

    result.setdefault("errors", [])
    result["errors"] = list(dict.fromkeys(runtime_errors + list(result["errors"])))

    reply = str(result.get("reply") or "").strip()
    if not reply:
        empty_error = RuntimeError("empty_v2_reply")
        if platform_error:
            result = _unpersisted_contact_result(_error_label("platform", empty_error))
        else:
            result = _fallback_result(handoff=handoff, error=empty_error)
        result["errors"] = runtime_errors + list(result.get("errors") or [])
        reply = result["reply"]

    allowed = True
    delivery_outcome = "send_failed"
    if delivery_allowed is not None:
        try:
            allowed = bool(delivery_allowed())
            if not allowed:
                delivery_outcome = "stale_suppressed"
        except Exception as error:  # noqa: BLE001
            allowed = False
            delivery_outcome = "delivery_guard_failed"
            result["errors"].append(_error_label("delivery_guard", error))
    delivered = False
    outbox_message_id = 0
    outbox_already_sent = False
    if allowed:
        if conversation_id > 0:
            try:
                if existing_outbox and existing_outbox.get("status") in {"prepared", "failed"}:
                    prepared = existing_outbox
                elif prepare_message is not None:
                    prepared = prepare_message(
                        conversation_id,
                        correlation_id,
                        reply,
                        _outbox_action_for_result(result),
                    )
                elif record_message is not None:
                    # Compatibility for isolated unit callers. Production uses
                    # the idempotent prepared/sent outbox callbacks.
                    record_message(conversation_id, reply)
                    prepared = {"message_id": 0, "body": reply, "status": "prepared"}
                else:
                    raise RuntimeError("v2 response persistence unavailable")
                reply = str(prepared.get("body") or reply).strip()
                outbox_message_id = int(prepared.get("message_id") or 0)
                outbox_already_sent = prepared.get("status") == "sent"
                stored_action = {
                    "operation": prepared.get("handoff_operation"),
                    "handoff_id": prepared.get("handoff_id"),
                    "reason": prepared.get("handoff_reason"),
                }
                stored_action = {key: value for key, value in stored_action.items() if value}
                if (
                    prepare_message is not None
                    and not result.get("outbox_replay")
                    and (
                        reply != str(result.get("reply") or "").strip()
                        or stored_action != _outbox_action_for_result(result)
                    )
                ):
                    prior_errors = list(result.get("errors") or [])
                    prior_errors.append("outbox_canonical_response_reused")
                    result = _result_from_outbox(prepared, errors=prior_errors)
            except Exception as error:  # noqa: BLE001
                allowed = False
                delivery_outcome = "response_store_failed"
                result["errors"].append(_error_label("response_store", error))
        else:
            # Only the deterministic platform fallback can reach this state:
            # the inbound conversation itself could not be persisted.
            result["errors"].append("response_store_unavailable")

    if allowed and outbox_already_sent:
        delivered = True
        delivery_outcome = "sent"
    elif allowed:
        try:
            delivered = bool(send_message(customer_phone, reply))
            delivery_outcome = "sent" if delivered else "send_failed"
            if not delivered:
                result["errors"].append("whatsapp_send_failed")
        except Exception as error:  # noqa: BLE001
            delivery_outcome = "send_failed"
            result["errors"].append(_error_label("send", error))

    if outbox_message_id and finish_message is not None:
        try:
            finished = finish_message(
                conversation_id, outbox_message_id, correlation_id, delivered,
            )
            _apply_finish_result(result, finished)
            if not finished.get("found"):
                result["errors"].append("response_delivery_state_not_found")
        except Exception as error:  # noqa: BLE001
            result["errors"].append(_error_label("response_delivery_state", error))
    result["reply"] = reply

    # Compatibility only for isolated callers without the production outbox.
    # Production commits response delivery + handoff state in one DB transaction.
    if finish_message is None:
        prepared_ids = _prepared_handoffs(result)
        if prepared_ids:
            try:
                mark_v2_handoff_delivery(conversation_id, prepared_ids, delivered)
            except Exception as error:  # noqa: BLE001
                result["errors"].append(_error_label("handoff_delivery_state", error))

        resolution_allowed = delivered
        if resolution_allowed and delivery_allowed is not None:
            try:
                resolution_allowed = bool(delivery_allowed())
            except Exception as error:  # noqa: BLE001
                resolution_allowed = False
                result["errors"].append(_error_label("resolution_guard", error))
        for row in result.get("tool_results") or []:
            tool_result = row.get("result") or {}
            if not (
                row.get("name") == "handoff_to_isa"
                and tool_result.get("operation") == "resolve"
                and tool_result.get("resolution_prepared")
            ):
                continue
            tool_result["resolution_committed"] = False
            if not resolution_allowed:
                if delivered:
                    result["errors"].append("handoff_resolution_stale_suppressed")
                continue
            try:
                resolved = acknowledge_v2_handoff(
                    conversation_id, int(tool_result.get("handoff_id") or 0),
                )
                if resolved.get("found"):
                    tool_result.update(resolved)
                    tool_result["resolution_committed"] = True
                    tool_result["side_effect_executed"] = True
                else:
                    result["errors"].append("handoff_resolution_not_found")
            except Exception as error:  # noqa: BLE001
                result["errors"].append(_error_label("handoff_resolution_state", error))

    duration_ms = round((time.monotonic() - started) * 1000)
    decision_ms = max(0, round(float(result.get("latency_ms") or 0)))
    timings_ms = {
        "decision": decision_ms,
        "runtime_and_delivery": max(0, duration_ms - decision_ms),
    }
    events = _events_for_result(
        result=result,
        correlation_id=correlation_id,
        conversation_id=conversation_id,
        delivered=delivered,
        duration_ms=duration_ms,
        delivery_outcome=delivery_outcome,
    )
    try:
        record_v2_events(events)
    except Exception as error:  # noqa: BLE001
        print("[FredV2] analytics_error={}".format(type(error).__name__))
    print(
        "[FredV2] conversation={} delivered={} topic={} tools={} llm_calls={} "
        "latency_ms={} decision_ms={} runtime_and_delivery_ms={} path={}".format(
            conversation_id, str(delivered).lower(), _topic_for(result.get("tool_calls") or []),
            len(result.get("tool_calls") or []), result.get("model_calls") or 0, duration_ms,
            timings_ms["decision"], timings_ms["runtime_and_delivery"],
            result.get("fast_path") or "agent",
        )
    )
    return {
        **result,
        "delivered": delivered,
        "correlation_id": correlation_id,
        "duration_ms": duration_ms,
        "timings_ms": timings_ms,
        "events": events,
    }
