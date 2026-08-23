"""Fred v2: one semantic agent with a closed, validated domain-tool surface."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from v2_tools import ALLOWED_HANDOFF_REASONS, TOOL_SCHEMAS, V2ToolAdapters


MODEL = os.getenv("FRED_V2_MODEL", "deepseek-chat")
MODEL_URL = os.getenv("FRED_V2_MODEL_URL", "https://api.deepseek.com/chat/completions")
MAX_MODEL_CALLS = 5
MAX_TOOL_CALLS = 4
_TOOL_NAMES = {item["function"]["name"] for item in TOOL_SCHEMAS}

SYSTEM_PROMPT = """Sos Fred, el asistente de WhatsApp de Beauty House. Respondés en
español rioplatense, breve, cálido y natural. Vos sos el único componente que
interpreta el lenguaje y la intención del mensaje actual; el historial aporta
contexto, pero nunca reemplaza lo que la persona acaba de decir.

Tenés exactamente cuatro herramientas:
- search_knowledge: políticas/procedimientos aprobados. Usala para consultas del
  showroom, retiros, mayorista y demás información estable de Beauty House. Si
  responde allowed_next_action=reply, respondé la consulta: NO hagas handoff por
  el solo hecho de que una lista, precio o gestión posterior la confirme Isa.
- get_order: estado live de un pedido. Si preguntan por un pedido sin dar el
  número, pedilo de forma natural y no llames tools. Si el siguiente mensaje trae
  el número, usá el contexto y llamá get_order. Para logística, basá la respuesta
  en fulfillment_status, shipping_type, carrier y tracking del resultado.
- get_product: identidad real, variantes y disponibilidad live. Usala cuando
  preguntan si existe o está disponible un producto concreto. NUNCA la uses para
  asesoramiento, preferencias, necesidades, comparaciones o recomendaciones. Si
  devuelve status=not_found, tenés que llamar inmediatamente handoff_to_isa con
  reason=custom_order; no busques sustitutos ni cierres con “no tenemos”.
- handoff_to_isa: handoff por tema con link de WhatsApp elegido por la clienta;
  nunca le envía automáticamente un mensaje a Isa. Para crear uno usá
  operation=create. Hacé como máximo un create por turno; si un mensaje trae
  varios temas que requieren a Isa, resumilos brevemente dentro de ese único
  handoff. Compras, asesoramiento personalizado o algo que no se puede
  verificar con seguridad requieren esta tool. Una cantidad + producto concreto es intención de
  compra y va a Isa sin crear checkout. Una necesidad vaga que requiere elegir
  producto también es asesoramiento y va a Isa. Ante CUALQUIER pregunta subjetiva
  sobre qué elegir, qué conviene, qué sirve para un caso o qué tono/modelo queda
  mejor: llamá handoff_to_isa(reason=product_advice) de inmediato. No preguntes
  más, no recomiendes y no consultes catálogo. Esto no incluye identificar un
  producto que la clienta vio pero no puede nombrar, siempre que no esté pidiendo
  comprar: en ese caso de identificación, si faltan foto, link o nombre, pedí ese
  dato sin tools. Una solicitud explícita de comprar o llevar una cantidad va a
  handoff_to_isa(reason=purchase_intent) aunque todavía no hayas verificado la
  identidad; no pidas foto/link y no llames get_product. Un resultado
  status=simulated_success significa que el handoff se produciría en producción,
  aunque esta ejecución no hizo side effects; redactá la respuesta productiva.

Una solicitud de cambiar, cancelar, devolver o intervenir un pedido real es una
acción sensible: no la ejecutes, no consultes Knowledge ni intentes resolverla
con get_order. Llamá inmediatamente handoff_to_isa con reason=modify_order,
cancel_order, return_order o sensitive_order_action, respectivamente, y copiá
el número de pedido en order_number sólo si la persona lo dio. Una
solicitud explícita de hablar con Isa o atención humana tiene la misma prioridad,
incluso si el mensaje también contiene otra consulta.

Si Knowledge no confirma un procedimiento o detalle operativo, no lo completes
por inferencia: llamá handoff_to_isa(operation=create,
reason=operational_detail_unverified), explicando brevemente qué falta confirmar.

Los handoffs activos que recibas son estado por tema, no ownership de toda la
conversación. Un mensaje sobre otro tema se responde normalmente. Si preguntan
qué pasó con un tema ya derivado, no podés ver si escribieron a Isa ni lo que
resolvieron por ese canal: llamá handoff_to_isa(operation=repeat) con el id activo
para devolver esa aclaración y el acceso directo. Si la persona afirma claramente
que ya habló con Isa, llamá operation=resolve para ese id. Nunca supongas el
resultado de esa conversación externa.

Si preguntan en general qué podés hacer o cómo podés ayudar, respondé brevemente
sin tools y en una sola llamada: showroom/políticas, estado de pedidos con número,
información objetiva de productos y mayorista; para comprar o recibir asesoría,
podés derivar con Isa. Describí capacidades, no resultados live concretos.

Un saludo o cortesía se responde naturalmente sin herramientas. No deduzcas ni
inventes producto/SKU, pedido, stock, precio, tracking o acciones externas. No
uses tools para una cortesía, agradecimiento o confirmación breve como “dale” o
“perfecto”, aunque el historial anterior haya tratado un tema comercial. No
crees pedidos ni checkout ni generes links de producto o compra. Después de una
tool, redactá usando sólo su evidencia.
No expliques nombres internos de herramientas a la clienta."""


def make_model_call(*, timeout_seconds: float = 45.0) -> Callable[[List[Dict[str, Any]]], Dict[str, Any]]:
    """Build the same v2 model client with a caller-owned network deadline."""
    def call(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("Falta DEEPSEEK_API_KEY en las variables de entorno.")
        response = requests.post(
            MODEL_URL,
            headers={"Authorization": "Bearer {}".format(api_key), "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
                "temperature": 0.2,
            },
            timeout=max(1.0, float(timeout_seconds)),
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices or not choices[0].get("message"):
            raise RuntimeError("El modelo v2 devolvió una respuesta vacía.")
        message = dict(choices[0]["message"])
        message["_usage"] = payload.get("usage") or {}
        return message

    return call


def _default_model_call(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    return make_model_call()(messages)


def _history_messages(history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, str]]:
    safe = []
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            safe.append({"role": role, "content": content[:1200]})
    return safe


def _tool_call_record(call: Dict[str, Any], arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": call.get("function", {}).get("name", ""), "arguments": arguments}


def _safe_failed_tool_call(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name if name in _TOOL_NAMES else "unknown", "arguments": arguments}


def _decision_from_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not calls:
        return {"action": "reply", "source": "model"}
    last = calls[-1]
    if last["name"] == "handoff_to_isa":
        operation = last["arguments"].get("operation", "create")
        if operation == "resolve":
            return {"action": "handoff_resolved", "source": "handoff_to_isa"}
        if operation == "repeat":
            return {"action": "reply", "source": "handoff_repeat"}
        return {
            "action": "handoff_to_isa",
            "reason": last["arguments"].get("reason", "unable_to_verify"),
        }
    return {"action": "reply", "source": last["name"]}


class V2AgentExecutionError(RuntimeError):
    """Sanitized fail-closed error carrying only bounded measurement state."""

    def __init__(
        self,
        *,
        error_code: str,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Dict[str, Any]],
        model_calls: int,
        usage: Dict[str, int],
        fallback_reason: str = "unable_to_verify",
        fallback_summary: str = "Fred no pudo confirmar la consulta con seguridad.",
    ) -> None:
        super().__init__("Fred v2 execution failed")
        self.error_code = str(error_code or "agent_failure")[:120]
        self.tool_calls = list(tool_calls)
        self.tool_results = list(tool_results)
        self.model_calls = max(0, int(model_calls))
        self.fallback_reason = str(fallback_reason or "unable_to_verify")[:80]
        self.fallback_summary = str(fallback_summary or "")[:240]
        self.usage = {
            key: max(0, int(usage.get(key) or 0))
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }


class FredV2Agent:
    def __init__(
        self,
        *,
        model_call: Callable[[List[Dict[str, Any]]], Dict[str, Any]] = _default_model_call,
        tools: Optional[V2ToolAdapters] = None,
    ) -> None:
        self._model_call = model_call
        self._tools = tools or V2ToolAdapters()

    def answer(
        self,
        user_message: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
        active_handoffs: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *_history_messages(history),
            *([{
                "role": "system",
                "content": (
                    "ACTIVE TOPIC-SCOPED HANDOFFS (structured state, not proof of any "
                    "external outcome): {}"
                ).format(json.dumps(active_handoffs, ensure_ascii=False, default=str)),
            }] if active_handoffs else []),
            {
                "role": "system",
                "content": (
                    "CURRENT TURN: the next user message is the only request to solve now. "
                    "Earlier turns may resolve a genuinely dependent reference, but must not "
                    "supply intent, topic, product, order, or requested action to a new topic."
                ),
            },
            {"role": "user", "content": str(user_message or "").strip()},
        ]
        calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        errors: List[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        required_next_tool: Optional[Dict[str, str]] = None
        safe_order_reply = ""
        safe_product_reply = ""
        safe_custom_order_reply = ""
        safe_handoff_reply = ""

        for model_call_number in range(1, MAX_MODEL_CALLS + 1):
            try:
                message = self._model_call(messages)
            except Exception as error:  # noqa: BLE001
                raise V2AgentExecutionError(
                    error_code="model_error:{}".format(type(error).__name__),
                    tool_calls=calls,
                    tool_results=tool_results,
                    model_calls=model_call_number,
                    usage=usage,
                    fallback_reason=(
                        required_next_tool["reason"]
                        if required_next_tool else "unable_to_verify"
                    ),
                ) from error
            for key in usage:
                usage[key] += int((message.get("_usage") or {}).get(key) or 0)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                if required_next_tool:
                    messages.append({
                        "role": "system",
                        "content": (
                            "No podés responder todavía. El resultado estructurado anterior "
                            "exige llamar {} con operation=create y reason={}. Hacelo ahora."
                        ).format(required_next_tool["name"], required_next_tool["reason"]),
                    })
                    continue
                reply = str(message.get("content") or "").strip()
                if safe_order_reply:
                    reply = safe_order_reply
                if safe_product_reply:
                    reply = safe_product_reply
                if safe_custom_order_reply:
                    reply = safe_custom_order_reply
                if safe_handoff_reply:
                    reply = safe_handoff_reply
                if not reply:
                    raise V2AgentExecutionError(
                        error_code="empty_model_reply",
                        tool_calls=calls,
                        tool_results=tool_results,
                        model_calls=model_call_number,
                        usage=usage,
                        fallback_reason=(
                            required_next_tool["reason"]
                            if required_next_tool else "unable_to_verify"
                        ),
                    )
                return {
                    "reply": reply,
                    "tool_calls": calls,
                    "tool_results": tool_results,
                    "model_calls": model_call_number,
                    "latency_ms": round((time.monotonic() - started) * 1000, 2),
                    "errors": errors,
                    "usage": usage,
                    "decision": _decision_from_calls(calls),
                }

            messages.append({
                "role": "assistant",
                "content": message.get("content"),
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("argumentos deben ser un objeto")
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    safe_name = name if name in _TOOL_NAMES else "unknown"
                    failed_call = _safe_failed_tool_call(name, {})
                    raise V2AgentExecutionError(
                        error_code="invalid_arguments:{}".format(safe_name),
                        tool_calls=calls + [failed_call],
                        tool_results=tool_results + [{
                            "name": safe_name, "result": {"error": "invalid_arguments"},
                        }],
                        model_calls=model_call_number,
                        usage=usage,
                        fallback_reason=(
                            required_next_tool["reason"]
                            if required_next_tool else "unable_to_verify"
                        ),
                    ) from error
                else:
                    if len(calls) >= MAX_TOOL_CALLS:
                        safe_name = name if name in _TOOL_NAMES else "unknown"
                        raise V2AgentExecutionError(
                            error_code="tool_limit:{}".format(safe_name),
                            tool_calls=calls + [_safe_failed_tool_call(name, arguments)],
                            tool_results=tool_results + [{
                                "name": safe_name, "result": {"error": "tool_limit"},
                            }],
                            model_calls=model_call_number,
                            usage=usage,
                            fallback_reason=(
                                required_next_tool["reason"]
                                if required_next_tool
                                else (
                                    arguments.get("reason")
                                    if name == "handoff_to_isa"
                                    and arguments.get("reason") in ALLOWED_HANDOFF_REASONS
                                    else "unable_to_verify"
                                )
                            ),
                        )
                    else:
                        try:
                            if required_next_tool and (
                                name != required_next_tool["name"]
                                or arguments.get("reason") != required_next_tool["reason"]
                                or str(arguments.get("operation") or "create") != "create"
                            ):
                                raise ValueError(
                                    "tool, operation o reason no permitido por evidencia previa"
                                )
                            result = self._tools.call(name, arguments)
                        except Exception as error:  # noqa: BLE001
                            # No evidence tool may fail and then return control to
                            # free model prose. That could fabricate stock, price,
                            # order state, Knowledge details or contact with Isa.
                            # The runtime catches this sanitized error and emits the
                            # deterministic v2 contact fallback; v1 never runs.
                            safe_name = name if name in _TOOL_NAMES else "unknown"
                            raise V2AgentExecutionError(
                                error_code="tool_error:{}:{}".format(
                                    safe_name, type(error).__name__,
                                ),
                                tool_calls=calls + [_safe_failed_tool_call(name, arguments)],
                                tool_results=tool_results + [{
                                    "name": safe_name,
                                    "result": {"error": "tool_unavailable"},
                                }],
                                model_calls=model_call_number,
                                usage=usage,
                                fallback_reason=(
                                    required_next_tool["reason"]
                                    if required_next_tool
                                    else (
                                        arguments.get("reason")
                                        if name == "handoff_to_isa"
                                        and arguments.get("reason") in ALLOWED_HANDOFF_REASONS
                                        else "unable_to_verify"
                                    )
                                ),
                            ) from error
                calls.append(_tool_call_record(call, arguments))
                tool_results.append({"name": name, "result": result})
                if name == "get_product" and result.get("status") == "not_found":
                    required_next_tool = {
                        "name": "handoff_to_isa", "operation": "create",
                        "reason": "custom_order",
                    }
                    safe_custom_order_reply = str(result.get("customer_safe_reply") or "")
                if name == "get_product" and result.get("status") == "found":
                    safe_product_reply = str(result.get("customer_safe_reply") or "")
                if (
                    name == "search_knowledge"
                    and result.get("allowed_next_action") == "handoff_to_isa"
                ):
                    required_next_tool = {
                        "name": "handoff_to_isa",
                        "operation": "create",
                        "reason": "operational_detail_unverified",
                    }
                if name == "get_order" and result.get("customer_safe_reply"):
                    safe_order_reply = str(result["customer_safe_reply"])
                if (
                    name == "get_order"
                    and result.get("allowed_next_action") == "handoff_to_isa"
                ):
                    required_next_tool = {
                        "name": "handoff_to_isa", "operation": "create",
                        "reason": "unable_to_verify",
                    }
                if name == "handoff_to_isa" and result.get("customer_safe_reply"):
                    safe_handoff_reply = str(result["customer_safe_reply"])
                if (
                    name == "handoff_to_isa"
                    and required_next_tool
                    and str(arguments.get("operation") or "create") == "create"
                    and arguments.get("reason") == required_next_tool["reason"]
                    and not result.get("error")
                ):
                    required_next_tool = None
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "tool-{}".format(len(calls))),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
                if name == "handoff_to_isa" and safe_handoff_reply:
                    return {
                        "reply": safe_handoff_reply,
                        "tool_calls": calls,
                        "tool_results": tool_results,
                        "model_calls": model_call_number,
                        "latency_ms": round((time.monotonic() - started) * 1000, 2),
                        "errors": errors,
                        "usage": usage,
                        "decision": _decision_from_calls(calls),
                    }

        if safe_handoff_reply:
            return {
                "reply": safe_handoff_reply,
                "tool_calls": calls,
                "tool_results": tool_results,
                "model_calls": MAX_MODEL_CALLS,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "errors": errors + ["model_call_limit"],
                "usage": usage,
                "decision": _decision_from_calls(calls),
            }
        fallback_reason = (
            required_next_tool["reason"] if required_next_tool else "unable_to_verify"
        )
        raise V2AgentExecutionError(
            error_code="required_tool_missing:{}".format(fallback_reason)
            if required_next_tool else "model_call_limit",
            tool_calls=calls,
            tool_results=tool_results,
            model_calls=MAX_MODEL_CALLS,
            usage=usage,
            fallback_reason=fallback_reason,
            fallback_summary=(
                "La fuente real requiere derivar este caso y Fred no pudo completar "
                "el handoff dentro del turno."
            ),
        )


def answer(
    user_message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    active_handoffs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return FredV2Agent().answer(
        user_message, history=history, active_handoffs=active_handoffs,
    )
