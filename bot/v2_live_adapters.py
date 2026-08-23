"""Live v2 handoff adapter: customer-owned wa.me link, never a message to Isa."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, Iterable
from urllib.parse import quote

from operations_store import record_v2_handoff
from privacy import redact_text


ISA_PUBLIC_E164 = "5491124528750"
ISA_PUBLIC_DISPLAY = "+54 9 11 2452-8750"
MAX_HANDOFF_CONTEXT_CHARS = 280

REASON_LABELS = {
    "custom_order": "consultar un producto por encargo",
    "human_request": "hablar con Isa",
    "purchase_intent": "coordinar una compra",
    "product_advice": "recibir asesoramiento de producto",
    "unable_to_verify": "revisar un dato que Fred no pudo confirmar",
    "cancel_order": "cancelar pedido",
    "modify_order": "modificar pedido",
    "return_order": "devolver pedido",
    "sensitive_order_action": "gestionar un pedido con intervención de Isa",
    "operational_detail_unverified": "confirmar un detalle operativo",
}


def _configured_isa_number() -> str:
    digits = "".join(char for char in os.getenv("ISA_WHATSAPP_NUMBER", "") if char.isdigit())
    if digits != ISA_PUBLIC_E164:
        raise RuntimeError(
            "ISA_WHATSAPP_NUMBER debe ser 5491124528750 para habilitar handoff v2."
        )
    return digits


def _compact_context(value: Any) -> str:
    compact = " ".join(redact_text(value, limit=MAX_HANDOFF_CONTEXT_CHARS).split())
    return compact[:MAX_HANDOFF_CONTEXT_CHARS]


def _handoff_message(reason_label: str, order_number: str, context_summary: str) -> str:
    lines = [
        "Hola Isa, Fred me recomendó hablar con vos.",
        "",
        "Motivo: {}".format(reason_label),
    ]
    if order_number:
        lines.append("Pedido: #{}".format(order_number))
    if context_summary:
        lines.append("Contexto: {}".format(context_summary))
    return "\n".join(lines)


def _canonical_context(reason: str, order_number: str = "") -> str:
    order_actions = {
        "cancel_order": "quiero cancelar el pedido",
        "modify_order": "quiero modificar el pedido",
        "return_order": "quiero devolver el pedido",
        "sensitive_order_action": "necesito ayuda con el pedido",
    }
    if order_number:
        lead = "{} #{}".format(
            order_actions.get(reason, "necesito ayuda con el pedido"), order_number,
        )
    else:
        lead = "necesito ayuda para {}".format(
            REASON_LABELS.get(reason, "revisar el caso")
        )
    return "{} y Fred me indicó que este tema necesita tu intervención.".format(lead)


def build_isa_wa_url(
    *, reason_label: str, order_number: str = "", context_summary: str = "",
) -> str:
    number = _configured_isa_number()
    message = _handoff_message(
        _compact_context(reason_label),
        _compact_context(order_number),
        _compact_context(context_summary),
    )
    return "https://wa.me/{}?text={}".format(number, quote(message, safe=""))


def _customer_reply(lead: str, wa_url: str, *, repeated: bool = False) -> str:
    visibility = ""
    if repeated:
        visibility = (
            "No puedo ver si ya hablaste con Isa ni qué resolvieron por ese canal. "
            "Si todavía no le escribiste, te vuelvo a pasar el acceso directo.\n\n"
        )
    return (
        "{}{}\n\nWhatsApp de Isa: {}\n{}"
    ).format(visibility, lead, ISA_PUBLIC_DISPLAY, wa_url)


def _pending_by_id(pending_handoffs: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    return {
        int(item["id"]): dict(item)
        for item in pending_handoffs
        if item.get("id") is not None
    }


def live_handoff_adapter(
    *,
    conversation_id: int,
    correlation_id: str,
    pending_handoffs: Iterable[Dict[str, Any]] = (),
    create_handoff: Callable[..., Dict[str, Any]] = record_v2_handoff,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build the only mutable v2 tool; it writes state, never contacts Isa."""
    pending = _pending_by_id(pending_handoffs)
    created_result: Dict[str, Any] = {}

    def handoff(payload: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal created_result
        operation = str(payload.get("operation") or "create")
        if operation in {"repeat", "resolve"}:
            handoff_id = int(payload.get("handoff_id") or 0)
            existing = pending.get(handoff_id)
            if not existing:
                raise ValueError("handoff_id no pertenece a esta conversación")
            if operation == "resolve":
                return {
                    "found": True,
                    "id": handoff_id,
                    "handoff_id": handoff_id,
                    "reason": existing["reason"],
                    "order_number": existing.get("order_number") or None,
                    "status": "pending",
                    "operation": "resolve",
                    "resolution_prepared": True,
                    "resolution_committed": False,
                    "side_effect_executed": False,
                    "message_sent_to_isa": False,
                    "customer_safe_reply": (
                        "Gracias por avisarme. Si necesitás otra cosa, "
                        "sigo disponible por acá."
                    ),
                }

            existing_reason = str(existing["reason"])
            existing_order = str(existing.get("order_number") or "")
            url = build_isa_wa_url(
                reason_label=REASON_LABELS[existing_reason],
                order_number=existing_order,
                context_summary=_canonical_context(existing_reason, existing_order),
            )
            return {
                "accepted": True,
                "status": "pending",
                "operation": "repeat",
                "handoff_id": handoff_id,
                "reason": existing["reason"],
                "wa_url": url,
                "visible_number": ISA_PUBLIC_DISPLAY,
                "side_effect_executed": False,
                "message_sent_to_isa": False,
                "customer_safe_reply": _customer_reply(
                    "Ese tema lo tiene que revisar Isa.", url, repeated=True,
                ),
            }

        # A customer turn owns at most one newly-created handoff. The agent
        # returns immediately after its deterministic reply, and this guard
        # prevents a direct/retried second call from overwriting that topic.
        if created_result:
            return {**created_result, "reused_existing": True}

        reason = str(payload.get("reason") or "")
        if reason not in REASON_LABELS:
            raise ValueError("reason de handoff desconocido")
        reason_label = REASON_LABELS[reason]
        order_number = _compact_context(payload.get("order_number") or "")
        context_summary = _canonical_context(reason, order_number)
        state = create_handoff(
            correlation_id=correlation_id,
            conversation_id=int(conversation_id),
            reason=reason,
            order_number=order_number,
        )
        # On an idempotent retry, Postgres may preserve an already-pending
        # record. Build the link from the returned canonical state so the
        # customer never receives context that disagrees with stored state.
        stored_reason = str(state.get("reason") or reason)
        stored_order_number = str(state.get("order_number") or order_number)
        stored_reason_label = REASON_LABELS[stored_reason]
        stored_context_summary = _canonical_context(stored_reason, stored_order_number)
        url = build_isa_wa_url(
            reason_label=stored_reason_label,
            order_number=stored_order_number,
            context_summary=stored_context_summary,
        )
        created_result = {
            "accepted": True,
            "status": state.get("status") or "prepared",
            "operation": "create",
            "handoff_id": state["id"],
            "reason": stored_reason,
            "order_number": stored_order_number or None,
            "wa_url": url,
            "visible_number": ISA_PUBLIC_DISPLAY,
            "side_effect_executed": True,
            "message_sent_to_isa": False,
            "customer_safe_reply": _customer_reply(
                "Ese tema lo tiene que revisar Isa.", url,
            ),
        }
        return dict(created_result)

    return handoff
