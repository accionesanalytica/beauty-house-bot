"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, registra su historial en Supabase y, por ahora,
responde con una plantilla de prueba aprobada por Meta.
"""

import os
import re
import sys
import unicodedata

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
from tiendanube_draft_orders import DraftOrderDemoError, create_demo_draft_order
from conversation_store import (
    cancel_sales_intake,
    create_pending_action,
    get_active_sales_intake,
    load_history,
    list_pending_actions,
    mark_sales_intake_ready,
    pending_action_count,
    record_isa_feedback,
    record_bot_message,
    record_inbound_message,
    resolve_pending_action,
    set_sales_intake_customer,
    set_sales_intake_fulfillment,
    set_sales_intake_product,
    set_sales_intake_quantity,
    set_conversation_state,
    start_sales_intake,
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
SALES_INTAKE_ENABLED = os.getenv("SALES_INTAKE_ENABLED", "false").lower() == "true"

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
    sale_draft = action["payload"].get("sale_draft")
    if sale_draft:
        text += (
            "\n\nBorrador de venta"
            "\nProducto/variante/cantidad: {}"
            "\nEntrega: {}"
            "\nPago: {}"
        ).format(
            sale_draft["items_status"],
            sale_draft["delivery_status"],
            sale_draft["payment_status"],
        )

        context = action["payload"].get("conversation_context", [])
        if context:
            compact_context = " | ".join(
                "{}: {}".format(item["speaker"], item["body"])
                for item in context[-4:]
            )
            text += "\nContexto: {}".format(compact_context)
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
                    {"type": "reply", "reply": {"id": "approve:{}".format(action_id), "title": "Tomar caso"}},
                    {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Descartar"}},
                    {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
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
    conversation_context: list = None,
    sale_draft: dict = None,
) -> int:
    """Create an escalation and notify Isa only when the queue was empty."""
    pending_before = pending_action_count()
    payload = {"customer_phone": customer_phone, "customer_message": customer_message}
    if action_type == "purchase_review":
        # A draft is an internal checklist, not an order. Fields are populated
        # only later, after Isa reviews the conversation and explicitly approves.
        payload["sale_draft"] = sale_draft or {
            "status": "needs_isa_review",
            "items_status": "por confirmar",
            "delivery_status": "por confirmar",
            "payment_status": "por confirmar",
            "order_creation": "disabled",
        }
        payload["conversation_context"] = [
            {
                "speaker": "Clienta" if item.get("role") == "user" else "Fred",
                "body": (item.get("content") or "")[:240],
            }
            for item in (conversation_context or [])[-6:]
            if item.get("content")
        ]
    action_id = create_pending_action(
        conversation_id=conversation_id,
        action_type=action_type,
        summary=summary,
        payload=payload,
    )
    set_conversation_state(conversation_id, "ESCALATED")
    if pending_before == 0:
        send_isa_pending_notification(pending_before + 1)
    print(f"[Isa] Pendiente #{action_id} creado ({action_type}).")
    return action_id


def _customer_escalation_type(message_text: str, has_bot_history: bool) -> str:
    """Recognize only clear handoff/purchase signals; vague questions stay with Fred."""
    normalized = message_text.lower()
    if re.search(r"\bno\b.{0,30}\b(quiero|interesa|comprar|proceder)\b", normalized):
        return ""
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
        r"\bproceder con la compra\b",
    )
    if has_bot_history and any(re.search(pattern, normalized) for pattern in purchase_patterns):
        return "purchase_review"
    return ""


def _needs_purchase_clarification(message_text: str, prior_history: list) -> bool:
    """Avoid guessing after the client has just rejected a proposed product."""
    wants_to_proceed = re.search(
        r"\b(lo quiero|me lo llevo|quiero comprar|quiero hacer el pedido|"
        r"(?:me )?gustar[ií]a proceder con la compra|quiero proceder con la compra)\b",
        message_text,
        flags=re.IGNORECASE,
    )
    if not wants_to_proceed:
        return False

    recent_customer_messages = [
        item.get("content", "")
        for item in prior_history[-4:]
        if item.get("role") == "user"
    ]
    if not recent_customer_messages:
        return False

    last_customer_message = recent_customer_messages[-1]
    return bool(
        re.search(
            r"\bno\b.{0,80}\b(quiero|interesa|avanzar|proceder|comprar)\b",
            last_customer_message,
            flags=re.IGNORECASE,
        )
    )


def _normalized_text(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(character) != "Mn"
    )


def _extract_quantity(text: str) -> int:
    match = re.search(r"\b(\d{1,2})\s*(?:x|u|unidades?|unidad)?\b", text.lower())
    if not match:
        return 0
    quantity = int(match.group(1))
    return quantity if 1 <= quantity <= 99 else 0


def _extract_customer_details(text: str) -> tuple:
    email_match = re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", text)
    if not email_match:
        return ()

    name_text = text[:email_match.start()]
    name_text = re.sub(r"(?i)\b(nombre|soy|mi mail|email|correo|es)\b\s*:? *", "", name_text)
    name_text = re.sub(r"[,:;|]+", " ", name_text).strip()
    name_words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", name_text)
    if len(name_words) < 2:
        return ()
    return " ".join(name_words[:5]), email_match.group(0).lower()


def _sales_fulfillment(text: str) -> str:
    normalized = _normalized_text(text)
    if any(word in normalized for word in ("retiro", "retirar", "showroom", "paso a buscar")):
        return "pickup"
    if any(word in normalized for word in ("envio", "enviar", "domicilio", "correo")):
        return "shipping"
    return ""


def _sales_summary(intake: dict) -> str:
    fulfillment = "envío" if intake["fulfillment"] == "shipping" else "retiro"
    return (
        "Te resumo antes de pasárselo a Isa:\n"
        "Producto/modelo: {}\n"
        "Variante: {}\n"
        "Cantidad: {}\n"
        "Entrega: {}\n"
        "Nombre: {}\n"
        "Email: {}\n\n"
        "¿Confirmás que lo preparemos para revisión?"
    ).format(
        intake["product_request"],
        intake["selected_variant"] or "a confirmar",
        intake["quantity"],
        fulfillment,
        intake["customer_name"],
        intake["customer_email"],
    )


def _start_sales_intake(conversation_id: int, sale_candidate: dict = None) -> str:
    if sale_candidate:
        product_request = sale_candidate["product_name"]
        selected_variant = sale_candidate.get("variant") or ""
        start_sales_intake(
            conversation_id,
            product_request=product_request,
            selected_sku=sale_candidate["sku"],
            selected_variant=selected_variant,
        )
        return "¡Buenísimo! ¿Cuántas unidades querés llevar?"

    start_sales_intake(conversation_id)
    return (
        "¡Dale! Para prepararte el link necesito confirmar bien el producto. "
        "¿Qué modelo o variante querés llevar?"
    )


def _handle_sales_intake(
    conversation_id: int,
    customer_phone: str,
    message_text: str,
    intake: dict,
    prior_history: list,
) -> bool:
    """Advance one safe sales-form step. Returns true when it sent a reply."""
    normalized = _normalized_text(message_text)
    if re.fullmatch(r"(?:cancelar|cancelo|dejalo|no sigo)", normalized):
        cancel_sales_intake(conversation_id)
        reply = "Dale, cancelé esta preparación. Si querés volver a empezar, avisame 😊"
    elif intake["status"] == "product":
        set_sales_intake_product(conversation_id, message_text)
        reply = "Perfecto. ¿Cuántas unidades querés?"
    elif intake["status"] == "quantity":
        quantity = _extract_quantity(message_text)
        if not quantity:
            reply = "Para confirmarlo bien, decime solo cuántas unidades querés."
        else:
            set_sales_intake_quantity(conversation_id, quantity)
            reply = "¿Preferís envío o retiro?"
    elif intake["status"] == "fulfillment":
        fulfillment = _sales_fulfillment(message_text)
        if not fulfillment:
            reply = "¿Lo necesitás con envío o preferís retirar?"
        else:
            set_sales_intake_fulfillment(conversation_id, fulfillment)
            reply = (
                "Último dato: pasame tu nombre y apellido junto con tu email. "
                "Ejemplo: Ana Pérez, ana@email.com"
            )
    elif intake["status"] == "customer":
        customer_details = _extract_customer_details(message_text)
        if not customer_details:
            reply = "Necesito nombre y apellido + un email válido. Ejemplo: Ana Pérez, ana@email.com"
        else:
            customer_name, customer_email = customer_details
            set_sales_intake_customer(conversation_id, customer_name, customer_email)
            refreshed = get_active_sales_intake(conversation_id)
            reply = _sales_summary(refreshed)
    elif intake["status"] == "confirmation":
        if re.match(r"^(si|confirmo|confirmar|dale|ok)\b", normalized):
            mark_sales_intake_ready(conversation_id)
            sale_draft = {
                "status": "ready_for_isa_review",
                "items_status": "{} × {}".format(
                    intake["quantity"], intake["product_request"]
                ),
                "selected_sku": intake["selected_sku"] or "a confirmar",
                "selected_variant": intake["selected_variant"] or "a confirmar",
                "delivery_status": "envío" if intake["fulfillment"] == "shipping" else "retiro",
                "payment_status": "link pendiente de aprobación de Isa",
                "customer_name": intake["customer_name"],
                "customer_email": intake["customer_email"],
                "order_creation": "disabled until Isa approval",
            }
            _queue_for_isa(
                conversation_id,
                customer_phone,
                "purchase_review",
                "La clienta confirmó una ficha de venta completa.",
                message_text,
                conversation_context=prior_history,
                sale_draft=sale_draft,
            )
            reply = "Perfecto, ya se lo pasé a Isa para que revise los detalles antes de generar cualquier link 😊"
        elif re.match(r"^(no|cambiar|corregir)\b", normalized):
            reply = _start_sales_intake(conversation_id)
        else:
            reply = "¿Confirmás el resumen? Respondé “confirmo” o decime si querés corregirlo."
    else:
        return False

    if send_whatsapp_text(customer_phone, reply):
        record_bot_message(conversation_id, reply)
    return True


def _isa_feedback_text(message_text: str) -> str:
    """Extract explicit internal feedback without treating normal messages as feedback."""
    match = re.match(r"^\s*feedback\s*:\s*(.+)$", message_text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _isa_demo_order_request(message_text: str) -> tuple:
    """Parse Isa's explicit demo-only order command."""
    match = re.match(
        r"^\s*demo\s*:\s*([A-Za-z0-9_-]+)\s*[x×]\s*(\d+)\s*$",
        message_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ()
    return match.group(1), int(match.group(2))


def _is_demo_command(message_text: str) -> bool:
    return bool(re.match(r"^\s*demo\b", message_text, flags=re.IGNORECASE))


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

    demo_order_request = _isa_demo_order_request(message_text)
    if demo_order_request:
        sku, quantity = demo_order_request
        try:
            draft_order = create_demo_draft_order(sku, quantity)
        except DraftOrderDemoError as error:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No creé ninguna orden. {}".format(error),
            )
            return

        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Link demo creado ✅\nProducto: {}\nCantidad: {}\nBorrador #{}\n{}\n\n"
            "Es solo una prueba: no se lo envíes a una clienta ni lo uses para cobrar."
            .format(
                draft_order["product_name"],
                draft_order["quantity"],
                draft_order["id"],
                draft_order["checkout_url"],
            ),
        )
        print("[Isa] Borrador demo #{} creado.".format(draft_order["id"]))
        return

    if _is_demo_command(message_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Para la prueba demo escribí exactamente: demo: TEST-FRED-001 x 1\n"
            "No se crea nada si el modo demo está apagado.",
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
        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Tomaste el caso #{}. Fred deja de responderle a la clienta. Todavía "
            "no se creó ninguna orden: es solo el borrador de trabajo para esta prueba."
            .format(action_id),
        )
    else:
        set_conversation_state(result["conversation_id"], "BOT")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Descartaste el pendiente #{}. Fred vuelve a atender a la clienta."
            .format(action_id),
        )

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

        if SALES_INTAKE_ENABLED:
            try:
                active_sales_intake = get_active_sales_intake(conversation_id)
                if active_sales_intake and _handle_sales_intake(
                    conversation_id,
                    customer_phone,
                    message_text,
                    active_sales_intake,
                    prior_history,
                ):
                    return JSONResponse(content={"ok": True})
            except Exception as error:  # noqa: BLE001
                print(f"ERROR en ficha de venta (tipo: {type(error).__name__})")
                customer_reply = (
                    "Perdón, no pude preparar esos datos ahora. Se lo paso a Isa para revisarlo."
                )
                _queue_for_isa(
                    conversation_id,
                    customer_phone,
                    "bot_fallback",
                    "Fred no pudo guardar la ficha de venta.",
                    message_text,
                    conversation_context=prior_history,
                )
                if send_whatsapp_text(customer_phone, customer_reply):
                    record_bot_message(conversation_id, customer_reply)
                return JSONResponse(content={"ok": True})

        if _needs_purchase_clarification(message_text, prior_history):
            customer_reply = (
                "Para no confundirme: el set sorpresa lo dejamos descartado. "
                "¿Querés que busquemos otra opción natural o había otro modelo "
                "puntual con el que querías avanzar? 😊"
            )
            if send_whatsapp_text(customer_phone, customer_reply):
                record_bot_message(conversation_id, customer_reply)
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
                conversation_context=prior_history,
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

            sale_candidate = result.get("sale_candidate")
            handoff = result.get("handoff")
            if sale_candidate and SALES_INTAKE_ENABLED:
                try:
                    reply = _start_sales_intake(conversation_id, sale_candidate)
                    handoff = None
                except Exception as error:  # noqa: BLE001
                    print(f"ERROR iniciando ficha preseleccionada (tipo: {type(error).__name__})")
                    handoff = {
                        "reason": "unable_to_verify",
                        "summary": "Fred no pudo guardar la selección de compra.",
                    }

            if handoff:
                action_type = (
                    "purchase_review"
                    if handoff.get("reason") == "purchase_intent"
                    else "human_handoff"
                    if handoff.get("reason") == "human_request"
                    else "bot_fallback"
                )
                if action_type == "purchase_review" and SALES_INTAKE_ENABLED:
                    reply = _start_sales_intake(conversation_id)
                else:
                    _queue_for_isa(
                        conversation_id,
                        customer_phone,
                        action_type,
                        handoff.get("summary") or "Fred solicitó intervención de Isa.",
                        message_text,
                        conversation_context=prior_history,
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
                conversation_context=prior_history,
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
    set_sales_intake_customer,
    set_sales_intake_fulfillment,
    set_sales_intake_product,
    set_sales_intake_quantity,
    start_sales_intake,
