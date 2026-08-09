"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, registra su historial en Supabase y, por ahora,
responde con una plantilla de prueba aprobada por Meta.
"""

import os
import re
import sys

# Agregar el directorio actual al path para importar agent.py
sys.path.insert(0, os.path.dirname(__file__))

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import numpy as np
from google import genai
from google.genai import types

from agent import answer
from conversation_store import (
    create_pending_action,
    load_history,
    list_pending_actions,
    pending_action_count,
    record_isa_feedback,
    record_bot_message,
    record_inbound_message,
    resolve_pending_action,
    set_conversation_state,
)

load_dotenv()

app = FastAPI()

# ============================================================
# CONFIGURACIÓN
# ============================================================

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Seguridad: hasta completar las pruebas, el webhook conserva la plantilla
# actual. El modo agent se habilitará explícitamente en una etapa posterior.
BOT_RESPONSE_MODE = os.getenv("BOT_RESPONSE_MODE", "template").lower()
ISA_WHATSAPP_NUMBER = os.getenv("ISA_WHATSAPP_NUMBER", "")

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768

gemini_client = genai.Client(api_key=GEMINI_KEY)


# ============================================================
# GEMINI EMBEDDINGS
# ============================================================

def embed_text(text: str, task_type: str = "RETRIEVAL_QUERY") -> list:
    """Genera embedding de 768 dimensiones, normalizado."""

    result = gemini_client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIMS,
            task_type=task_type,
        ),
    )

    vector = np.array(result.embeddings[0].values)

    normalized = (
        vector / np.linalg.norm(vector)
    ).tolist()

    return normalized


# ============================================================
# BÚSQUEDA RAG EN SUPABASE
# ============================================================

def search_similar_products(query: str, limit: int = 3) -> str:
    """
    Busca productos similares en Supabase usando búsqueda vectorial.
    Devuelve un string con el contexto para el agent.
    """

    try:
        embedding = embed_text(
            query,
            task_type="RETRIEVAL_QUERY"
        )

        conn = psycopg2.connect(SUPABASE_DB_URL)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                product_id,
                variant_id,
                sku,
                product_name,
                variant,
                1 - (embedding <=> %s::vector) AS similarity
            FROM product_embeddings
            WHERE published = true
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (
                str(embedding),
                str(embedding),
                limit,
            ),
        )

        results = cursor.fetchall()

        cursor.close()
        conn.close()

        if not results:
            return ""

        context = "Productos encontrados:\n"

        for row in results:

            (
                product_id,
                variant_id,
                sku,
                product_name,
                variant,
                similarity,
            ) = row

            context += (
                f"- product_id: {product_id}; {product_name} "
                f"(SKU: {sku or 'N/A'}) "
                f"Variante: {variant or 'default'} "
                f"(similitud: {similarity:.2f})\n"
            )

        return context

    except Exception as error:  # noqa: BLE001

        # Database-driver errors can echo the full connection string. Never
        # write them to logs because it may contain SUPABASE_DB_URL secrets.
        print(
            "ERROR en search_similar_products "
            f"(tipo: {type(error).__name__})"
        )

        return ""


# ============================================================
# WHATSAPP — ENVÍO DE MENSAJES
# ============================================================

def normalize_whatsapp_recipient(phone_number: str) -> str:
    """Usa el formato que Meta registra para números móviles argentinos."""

    # El webhook identifica móviles argentinos como 549..., mientras que
    # la lista de destinatarios de prueba de Meta los registra como 54....
    if phone_number.startswith("549"):
        return f"54{phone_number[3:]}"

    return phone_number


def send_escalacion_isa_template(
    phone_number: str,
    pending_inquiries: int = 1,
) -> bool:
    """Envía la plantilla Meta escalacion_isa con la cantidad de consultas."""

    url = (
        f"https://graph.facebook.com/v26.0/"
        f"{WHATSAPP_PHONE_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    recipient_phone = normalize_whatsapp_recipient(phone_number)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "template",
        "template": {
            "name": "escalacion_isa",
            "language": {
                "code": "es_AR"
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(pending_inquiries),
                        }
                    ],
                }
            ],
        },
    }

    try:

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=10,
        )

        print(
            f"[WhatsApp] HTTP {response.status_code}"
        )

        print(
            f"[WhatsApp] Response: {response.text}"
        )

        response.raise_for_status()

        return True

    except Exception as e:

        print(f"ERROR enviando plantilla a WhatsApp: {e}")

        return False


def send_whatsapp_text(phone_number: str, text: str) -> bool:
    """Send a text reply inside the customer-initiated 24-hour window."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(phone_number),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[WhatsApp] HTTP {response.status_code}")
        print(f"[WhatsApp] Response: {response.text}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando texto a WhatsApp: {type(error).__name__}")
        return False


def send_isa_pending_notification(pending_count: int) -> bool:
    """Notify Isa once when the queue changes from empty to non-empty."""
    if not ISA_WHATSAPP_NUMBER:
        print("ERROR avisando a Isa: falta ISA_WHATSAPP_NUMBER")
        return False
    return send_escalacion_isa_template(ISA_WHATSAPP_NUMBER, pending_count)


def _is_isa_phone(phone_number: str) -> bool:
    return bool(ISA_WHATSAPP_NUMBER) and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER)
    )


def _pending_action_text(action: dict) -> str:
    labels = {
        "human_handoff": "Clienta pidió hablar con Isa",
        "purchase_review": "Compra pendiente de confirmación",
        "bot_fallback": "Fred no pudo resolver la consulta",
    }
    customer_message = action["payload"].get("customer_message", "")
    text = (
        "Pendiente #{}\n{}\nCliente: {}\n{}".format(
            action["id"],
            labels.get(action["action_type"], action["action_type"]),
            action["customer_phone"],
            action["summary"],
        )
    )
    if customer_message:
        text += "\nMensaje: {}".format(customer_message)
    return text[:950]


def send_isa_pending_buttons(action: dict) -> bool:
    """Show one queued draft to Isa after she messages the bot."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    action_id = action["id"]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": _pending_action_text(action)},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "approve:{}".format(action_id), "title": "Aprobar"}},
                    {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Rechazar"}},
                    {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver detalle"}},
                ]
            },
        },
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[Isa] HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando botones a Isa: {type(error).__name__}")
        return False


def send_next_pending_to_isa() -> bool:
    pending_actions = list_pending_actions(limit=1)
    if not pending_actions:
        return send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No tenés pendientes para revisar. 😊")
    return send_isa_pending_buttons(pending_actions[0])


def _queue_for_isa(
    conversation_id: int,
    customer_phone: str,
    action_type: str,
    summary: str,
    customer_message: str,
) -> int:
    """Create an escalation and notify Isa only when the queue was empty."""
    pending_before = pending_action_count()
    action_id = create_pending_action(
        conversation_id=conversation_id,
        action_type=action_type,
        summary=summary,
        payload={"customer_phone": customer_phone, "customer_message": customer_message},
    )
    set_conversation_state(conversation_id, "ESCALATED")
    if pending_before == 0:
        send_isa_pending_notification(pending_before + 1)
    print(f"[Isa] Pendiente #{action_id} creado ({action_type}).")
    return action_id


def _customer_escalation_type(message_text: str, has_bot_history: bool) -> str:
    """Recognize only clear handoff/purchase signals; vague questions stay with Fred."""
    normalized = message_text.lower()
    human_patterns = (
        r"hablar con isa",
        r"pasame con isa",
        r"quiero a isa",
        r"quiero hablar con una persona",
        r"hablar con alguien",
    )
    if any(re.search(pattern, normalized) for pattern in human_patterns):
        return "human_handoff"

    purchase_patterns = (
        r"\blo quiero\b",
        r"\bme lo llevo\b",
        r"\bquiero comprar\b",
        r"\bquiero hacer el pedido\b",
    )
    if has_bot_history and any(re.search(pattern, normalized) for pattern in purchase_patterns):
        return "purchase_review"
    return ""


def _isa_feedback_text(message_text: str) -> str:
    """Extract explicit internal feedback without treating normal messages as feedback."""
    match = re.match(r"^\s*feedback\s*:\s*(.+)$", message_text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def handle_isa_message(
    message_text: str,
    wa_message_id: str = "",
    button_reply_id: str = "",
) -> None:
    """Any message from Isa opens the queue; button replies resolve one draft."""
    feedback = _isa_feedback_text(message_text)
    if feedback:
        try:
            saved = record_isa_feedback(
                ISA_WHATSAPP_NUMBER,
                feedback,
                wa_message_id=wa_message_id or None,
            )
            if saved:
                send_whatsapp_text(
                    ISA_WHATSAPP_NUMBER,
                    "Listo, guardé tu feedback para revisarlo. No cambia nada automáticamente.",
                )
            else:
                print("[Isa] Feedback duplicado ignorado.")
        except Exception as error:  # noqa: BLE001
            print(f"ERROR guardando feedback de Isa (tipo: {type(error).__name__})")
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude guardar ese feedback ahora. Probá enviarlo de nuevo más tarde.",
            )
        return

    match = re.match(r"^(approve|reject|view):(\d+)$", button_reply_id or "")
    if not match:
        send_next_pending_to_isa()
        return

    action, action_id_text = match.groups()
    action_id = int(action_id_text)
    if action == "view":
        actions = [item for item in list_pending_actions(limit=20) if item["id"] == action_id]
        if actions:
            send_isa_pending_buttons(actions[0])
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
        return

    result = resolve_pending_action(
        action_id,
        "approved" if action == "approve" else "rejected",
    )
    if not result:
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
        return

    if action == "approve":
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Aprobaste el pendiente #{}. La creación de la orden todavía está apagada "
            "hasta terminar esta prueba.".format(action_id),
        )
    else:
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Rechazaste el pendiente #{}.".format(action_id))

    if pending_action_count():
        send_next_pending_to_isa()


# ============================================================
# WEBHOOK — VERIFICACIÓN META
# ============================================================

@app.get("/webhook")
async def webhook_get(request: Request):
    """Verificación del webhook por parte de Meta."""

    verify_token = request.query_params.get(
        "hub.verify_token"
    )

    challenge = request.query_params.get(
        "hub.challenge"
    )

    if verify_token == WHATSAPP_WEBHOOK_VERIFY_TOKEN:

        return JSONResponse(
            content=int(challenge)
        )

    return JSONResponse(
        content={"error": "Invalid token"},
        status_code=403,
    )


# ============================================================
# WEBHOOK — MENSAJES ENTRANTES
# ============================================================

@app.post("/webhook")
async def webhook_post(request: Request):
    """Procesa mensajes entrantes de WhatsApp."""

    body = await request.json()

    # Meta envía un array de entries
    if (
        "entry" not in body
        or not body["entry"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    entry = body["entry"][0]

    # Verificar cambios
    if (
        "changes" not in entry
        or not entry["changes"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    change = entry["changes"][0]

    # Verificar mensajes
    if (
        "value" not in change
        or "messages" not in change["value"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    messages = change["value"]["messages"]

    if not messages:
        return JSONResponse(
            content={"ok": True}
        )

    msg = messages[0]

    customer_phone = msg.get("from")
    wa_message_id = msg.get("id")
    button_reply_id = (
        msg.get("interactive", {})
        .get("button_reply", {})
        .get("id", "")
    )

    message_text = (
        msg.get("text", {})
        .get("body", "")
        .strip()
    )

    if not message_text and button_reply_id:
        message_text = (
            msg.get("interactive", {})
            .get("button_reply", {})
            .get("title", "")
            .strip()
        )

    if not message_text:

        return JSONResponse(
            content={"ok": True}
        )

    if _is_isa_phone(customer_phone):
        if _isa_feedback_text(message_text):
            print("\n[Isa] Feedback recibido.")
        else:
            print(f"\n[Isa] {message_text or button_reply_id}")
        handle_isa_message(
            message_text,
            wa_message_id=wa_message_id or "",
            button_reply_id=button_reply_id,
        )
        return JSONResponse(content={"ok": True})

    print(
        f"\n[WhatsApp] "
        f"{customer_phone}: "
        f"{message_text}"
    )

    prior_history = []
    history_available = True
    if BOT_RESPONSE_MODE == "agent":
        try:
            # Load before storing the current message: answer() adds it once.
            prior_history = load_history(customer_phone)
        except Exception as error:  # noqa: BLE001
            history_available = False
            print(f"ERROR cargando conversacion (tipo: {type(error).__name__})")

    try:
        conversation_id, state, duplicate = record_inbound_message(
            customer_phone=customer_phone,
            body=message_text,
            wa_message_id=wa_message_id,
        )
        if duplicate:
            print("[Conversacion] Mensaje duplicado ignorado.")
            return JSONResponse(content={"ok": True})

        print(
            f"[Conversacion] Guardado en {conversation_id} "
            f"(estado: {state})."
        )
    except Exception as error:  # noqa: BLE001
        # Do not block the current template test if the history store is down.
        # Error details may contain database information, so log only its type.
        history_available = False
        print(f"ERROR guardando conversacion (tipo: {type(error).__name__})")

    # ========================================================
    # RESPUESTA
    # ========================================================

    if BOT_RESPONSE_MODE == "agent":
        # A database outage must not turn into a stateless AI conversation.
        if not history_available:
            send_whatsapp_text(
                customer_phone,
                "Perdón, no pude procesar tu consulta ahora. Se la paso a Isa.",
            )
            return JSONResponse(content={"ok": True})

        if state != "BOT":
            print(f"[Conversacion] El bot no responde en estado {state}.")
            return JSONResponse(content={"ok": True})

        escalation_type = _customer_escalation_type(
            message_text,
            has_bot_history=any(
                message.get("role") == "assistant"
                for message in prior_history
            ),
        )
        if escalation_type:
            if escalation_type == "human_handoff":
                customer_reply = "Dale, se lo paso a Isa para que te ayude. 😊"
                summary = "La clienta pidió hablar directamente con Isa."
            else:
                customer_reply = "Perfecto, se lo paso a Isa para que confirme los detalles de tu compra. 😊"
                summary = "La clienta indicó que quiere avanzar con una compra."

            _queue_for_isa(
                conversation_id,
                customer_phone,
                escalation_type,
                summary,
                message_text,
            )
            if send_whatsapp_text(customer_phone, customer_reply):
                record_bot_message(conversation_id, customer_reply)
            return JSONResponse(content={"ok": True})

        try:
            rag_context = search_similar_products(message_text)
            result = answer(
                message_text,
                history=prior_history,
                rag_context=rag_context,
                greeting_required=not any(
                    message.get("role") == "assistant"
                    for message in prior_history
                ),
                verbose=False,
            )
            reply = (result.get("reply") or "").strip()
            if not reply:
                raise RuntimeError("El agente no devolvió texto.")

            handoff = result.get("handoff")
            if handoff:
                action_type = (
                    "purchase_review"
                    if handoff.get("reason") == "purchase_intent"
                    else "human_handoff"
                    if handoff.get("reason") == "human_request"
                    else "bot_fallback"
                )
                _queue_for_isa(
                    conversation_id,
                    customer_phone,
                    action_type,
                    handoff.get("summary") or "Fred solicitó intervención de Isa.",
                    message_text,
                )

            if send_whatsapp_text(customer_phone, reply):
                record_bot_message(conversation_id, reply)
                print("[Conversacion] Respuesta del agente guardada.")
        except Exception as error:  # noqa: BLE001
            print(f"ERROR respondiendo con agente (tipo: {type(error).__name__})")
            customer_reply = "Perdón, no pude resolverlo ahora. Se la paso a Isa."
            _queue_for_isa(
                conversation_id,
                customer_phone,
                "bot_fallback",
                "Fred no pudo completar una respuesta verificada.",
                message_text,
            )
            if send_whatsapp_text(
                customer_phone,
                customer_reply,
            ):
                record_bot_message(conversation_id, customer_reply)

        return JSONResponse(content={"ok": True})

    # Modo seguro por defecto: plantilla Meta escalacion_isa con {{1}} = 1.
    send_escalacion_isa_template(
        customer_phone,
        pending_inquiries=1,
    )

    return JSONResponse(
        content={"ok": True}
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():
    """Health check para Railway."""

    return {
        "status": "ok"
    }


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv("PORT", 8000)
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
