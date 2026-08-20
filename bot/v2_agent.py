"""Minimal Fred v2: one semantic agent, four closed domain tools.

Nothing imports or calls this module from the production webhook yet.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from v2_tools import TOOL_SCHEMAS, V2ToolAdapters


MODEL = os.getenv("FRED_V2_MODEL", "deepseek-chat")
MODEL_URL = os.getenv("FRED_V2_MODEL_URL", "https://api.deepseek.com/chat/completions")
MAX_MODEL_CALLS = 5
MAX_TOOL_CALLS = 4

SYSTEM_PROMPT = """Sos Fred, el asistente de WhatsApp de Beauty House. Respondés en
español rioplatense, breve, cálido y natural. Vos sos el único componente que
interpreta el lenguaje y la intención del mensaje actual; el historial aporta
contexto, pero nunca reemplaza lo que la persona acaba de decir.

Tenés exactamente cuatro herramientas:
- search_knowledge: políticas/procedimientos aprobados. Usala para consultas del
  showroom, retiros y demás información estable de Beauty House.
- get_order: estado live de un pedido. Si preguntan por un pedido sin dar el
  número, pedilo de forma natural y no llames tools. Si el siguiente mensaje trae
  el número, usá el contexto y llamá get_order. Para logística, basá la respuesta
  en fulfillment_status, shipping_type, carrier y tracking del resultado.
- get_product: identidad real, variantes y disponibilidad live. Usala cuando
  preguntan si existe o está disponible un producto concreto.
- handoff_to_isa: compras, asesoramiento personalizado o algo que no se puede
  verificar con seguridad. Una cantidad + producto concreto es intención de
  compra y va a Isa sin crear checkout. Una necesidad vaga que requiere elegir
  producto también es asesoramiento y va a Isa. Si la tool responde mode=preview,
  decí que Isa puede ayudar; no afirmes que ya fue notificada ni que el caso fue
  enviado.

Un saludo o cortesía se responde naturalmente sin herramientas. No deduzcas ni
inventes producto/SKU, pedido, stock, precio, tracking o acciones externas. No
crees pedidos ni checkout. Después de una tool, redactá usando sólo su evidencia.
No expliques nombres internos de herramientas a la clienta."""


def _default_model_call(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
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
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices or not choices[0].get("message"):
        raise RuntimeError("El modelo v2 devolvió una respuesta vacía.")
    message = dict(choices[0]["message"])
    message["_usage"] = payload.get("usage") or {}
    return message


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


def _decision_from_calls(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not calls:
        return {"action": "reply", "source": "model"}
    last = calls[-1]
    if last["name"] == "handoff_to_isa":
        return {
            "action": "handoff_to_isa",
            "reason": last["arguments"].get("reason", "unable_to_verify"),
        }
    return {"action": "reply", "source": last["name"]}


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
    ) -> Dict[str, Any]:
        started = time.monotonic()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *_history_messages(history),
            {"role": "user", "content": str(user_message or "").strip()},
        ]
        calls: List[Dict[str, Any]] = []
        tool_results: List[Dict[str, Any]] = []
        errors: List[str] = []
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for model_call_number in range(1, MAX_MODEL_CALLS + 1):
            message = self._model_call(messages)
            for key in usage:
                usage[key] += int((message.get("_usage") or {}).get(key) or 0)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                reply = str(message.get("content") or "").strip()
                if not reply:
                    errors.append("empty_model_reply")
                    reply = "No pude responderte con seguridad. ¿Querés que lo vea Isa?"
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
                    arguments = {}
                    result = {"error": "argumentos inválidos"}
                    errors.append("invalid_arguments:{}".format(name))
                else:
                    if len(calls) >= MAX_TOOL_CALLS:
                        result = {"error": "límite de herramientas alcanzado"}
                        errors.append("tool_limit")
                    else:
                        try:
                            result = self._tools.call(name, arguments)
                        except Exception as error:  # noqa: BLE001
                            result = {"error": "herramienta no disponible"}
                            errors.append("tool_error:{}:{}".format(name, type(error).__name__))
                calls.append(_tool_call_record(call, arguments))
                tool_results.append({"name": name, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", "tool-{}".format(len(calls))),
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        return {
            "reply": "No pude resolverlo con seguridad. ¿Querés que lo vea Isa?",
            "tool_calls": calls,
            "tool_results": tool_results,
            "model_calls": MAX_MODEL_CALLS,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "errors": errors + ["model_call_limit"],
            "usage": usage,
            "decision": _decision_from_calls(calls),
        }


def answer(
    user_message: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return FredV2Agent().answer(user_message, history=history)
