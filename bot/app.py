"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, registra su historial en Supabase y, por ahora,
responde con una plantilla de prueba aprobada por Meta.
"""

import asyncio
import html
import os
import re
import secrets
import sys
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

# Agregar el directorio actual al path para importar agent.py
sys.path.insert(0, os.path.dirname(__file__))

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import numpy as np
from google import genai
from google.genai import types

from agent import answer
from tiendanube_checkout import CheckoutError, checkout_enabled, create_approved_checkout
from tiendanube_draft_orders import DraftOrderDemoError, create_demo_draft_order
from tiendanube_tools import get_stock
from tiendanube_credentials import (
    TiendanubeCredentialError,
    save_tiendanube_credential,
)
from conversation_store import (
    add_isa_sale_session_details,
    cancel_sales_intake,
    claim_daily_isa_reminder,
    claim_requested_isa_reminder,
    clear_isa_reminder_snooze,
    clear_isa_sale_session,
    create_pending_action,
    get_isa_sale_session,
    get_active_sales_intake,
    isa_reminders_snoozed,
    load_history,
    list_pending_actions,
    mark_sales_intake_ready,
    pending_action_count,
    pending_reminder_snapshot,
    record_isa_feedback,
    record_bot_message,
    record_inbound_message,
    resolve_pending_action,
    release_daily_isa_reminder,
    save_pending_action_checkout,
    set_sales_intake_customer,
    set_sales_intake_fulfillment,
    set_sales_intake_product,
    set_sales_intake_quantity,
    set_conversation_state,
    set_isa_sale_session_type,
    start_isa_sale_session,
    start_sales_intake,
    snooze_isa_reminders,
    wait_for_isa_response,
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
# Segunda llave explícita: incluso si existen credenciales demo, una aprobación
# normal nunca crea nada. Solo sirve para probar el recorrido completo.
DEMO_APPROVALS_ENABLED = (
    os.getenv("DEMO_APPROVALS_ENABLED", "false").lower() == "true"
    and os.getenv("TIENDANUBE_DRAFT_ORDERS_MODE", "disabled").lower() == "demo"
)
LIVE_CHECKOUTS_ENABLED = checkout_enabled()
ISA_REMINDERS_ENABLED = os.getenv("ISA_REMINDERS_ENABLED", "false").lower() == "true"
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
_reminder_task = None

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


def _business_reminder_time(now: datetime) -> bool:
    """Do not chase Isa at night; she can explicitly ask for a later reminder."""
    return (now.hour, now.minute) >= (10, 0) and (now.hour, now.minute) < (20, 30)


def run_isa_reminder_check(now: datetime = None) -> None:
    """Send at most two friendly, template-safe reminders per local day."""
    if not ISA_REMINDERS_ENABLED or not ISA_WHATSAPP_NUMBER:
        return
    now = now or datetime.now(ARGENTINA_TZ)
    snapshot = pending_reminder_snapshot()
    if not snapshot["count"]:
        return

    if claim_requested_isa_reminder(ISA_WHATSAPP_NUMBER):
        if not send_isa_pending_notification(snapshot["count"]):
            print("ERROR enviando recordatorio solicitado a Isa.")
        return

    if not _business_reminder_time(now) or isa_reminders_snoozed(ISA_WHATSAPP_NUMBER):
        return
    oldest = snapshot["oldest_created_at"]
    if not oldest:
        return
    age = now - oldest.astimezone(ARGENTINA_TZ)
    kind = "follow_up" if age >= timedelta(hours=2) else "gentle"
    if kind == "gentle" and age < timedelta(minutes=25):
        return
    if not claim_daily_isa_reminder(ISA_WHATSAPP_NUMBER, kind, now.date()):
        return
    if not send_isa_pending_notification(snapshot["count"]):
        release_daily_isa_reminder(ISA_WHATSAPP_NUMBER, kind, now.date())
        print("ERROR enviando recordatorio automático a Isa.")


async def _isa_reminder_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_isa_reminder_check)
        except Exception as error:  # noqa: BLE001
            print("ERROR en recordatorios de Isa (tipo: {}).".format(type(error).__name__))
        await asyncio.sleep(300)


@app.on_event("startup")
async def start_isa_reminders() -> None:
    global _reminder_task
    if ISA_REMINDERS_ENABLED and _reminder_task is None:
        _reminder_task = asyncio.create_task(_isa_reminder_loop())


@app.on_event("shutdown")
async def stop_isa_reminders() -> None:
    if _reminder_task:
        _reminder_task.cancel()


def _is_isa_phone(phone_number: str) -> bool:
    return bool(ISA_WHATSAPP_NUMBER) and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER)
    )


def _format_ars(value) -> str:
    """Format a verified ARS amount for people, never for calculations."""
    try:
        return "${:,.0f}".format(Decimal(str(value))).replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return "a confirmar"


def _pending_action_text(action: dict) -> str:
    labels = {
        "human_handoff": "Clienta pidió hablar con Isa",
        "purchase_review": "Compra pendiente de confirmación",
        "bot_fallback": "Fred necesita confirmar una consulta",
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
            "\nProducto/cantidad: {}"
            "\nVariante: {}"
            "\nSubtotal productos: {}"
            "\nEntrega: {}"
            "\nEnvío: a confirmar"
            "\nTotal final: a confirmar"
            "\nCliente: {}"
            "\nEmail: {}"
            "\nPago: {}"
        ).format(
            sale_draft["items_status"],
            sale_draft.get("selected_variant", "a confirmar"),
            _format_ars(sale_draft.get("products_subtotal")),
            sale_draft["delivery_status"],
            sale_draft.get("customer_name", "a confirmar"),
            sale_draft.get("customer_email", "a confirmar"),
            sale_draft["payment_status"],
        )
    return text[:900]


def send_isa_pending_buttons(action: dict) -> bool:
    """Show one queued draft to Isa after she messages the bot."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    action_id = action["id"]
    buttons = []
    if LIVE_CHECKOUTS_ENABLED and action["action_type"] == "purchase_review":
        buttons = [
            {"type": "reply", "reply": {"id": "approve_checkout:{}".format(action_id), "title": "Aprobar compra"}},
            {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Cancelar compra"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver detalles"}},
        ]
    elif DEMO_APPROVALS_ENABLED and action["action_type"] == "purchase_review":
        buttons = [
            {"type": "reply", "reply": {"id": "approve_demo:{}".format(action_id), "title": "Aprobar demo"}},
            {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Cancelar compra"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver detalles"}},
        ]
    elif action["action_type"] == "bot_fallback":
        buttons = [
            {"type": "reply", "reply": {"id": "reply_to_fred:{}".format(action_id), "title": "Responder a Fred"}},
            {"type": "reply", "reply": {"id": "resume_bot:{}".format(action_id), "title": "Que siga Fred"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
        ]
    elif action["action_type"] == "human_handoff":
        buttons = [
            {"type": "reply", "reply": {"id": "reply_to_fred:{}".format(action_id), "title": "Responder a Fred"}},
            {"type": "reply", "reply": {"id": "pause_bot:{}".format(action_id), "title": "Pausar a Fred"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
        ]
    else:
        buttons = [
            {"type": "reply", "reply": {"id": "approve:{}".format(action_id), "title": "Tomar caso"}},
            {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Volver a Fred"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
        ]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": _pending_action_text(action)},
            "action": {
                "buttons": buttons
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
        action = {
            "id": action_id,
            "action_type": action_type,
            "summary": summary,
            "payload": payload,
            "customer_phone": customer_phone,
        }
        # If Isa wrote recently, WhatsApp permits the detailed interactive card
        # immediately. Outside that window Meta rejects it, so fall back to the
        # approved template and wait for Isa to reply "ver".
        if not send_isa_pending_buttons(action):
            send_isa_pending_notification(pending_before + 1)
    print(f"[Isa] Pendiente #{action_id} creado ({action_type}).")
    return action_id


def _customer_escalation_type(message_text: str, has_bot_history: bool) -> str:
    """Recognize direct human handoffs; sales intake handles purchase intent."""
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
    # Con la ficha de venta activa, no saltamos el catálogo ni molestamos a Isa:
    # el agente debe identificar/verificar el producto y pedir solo los datos
    # faltantes. El pase a Isa ocurre recién después de la confirmación final.
    if (
        has_bot_history
        and not SALES_INTAKE_ENABLED
        and any(re.search(pattern, normalized) for pattern in purchase_patterns)
    ):
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
    # Prefer an explicit WhatsApp form label. It keeps a friendly message such
    # as "te dejo los datos" from becoming part of the customer's name.
    labeled_name = re.search(
        r"(?im)^\s*(?:nombre(?:\s+y\s+apellido)?|nombre completo)\s*:\s*([^\r\n,;|]+)",
        name_text,
    )
    if labeled_name:
        name_text = labeled_name.group(1)
    else:
        name_text = re.sub(
            r"(?i)^\s*(?:genial\s*[,:;-]*\s*)?(?:te\s+)?(?:dejo|paso|mando)?\s*"
            r"(?:los\s+)?(?:mis\s+)?datos\s*[:,-]*\s*",
            "",
            name_text,
        )
    name_text = re.sub(r"(?i)\b(nombre|soy|mi mail|email|correo|es)\b\s*:? *", "", name_text)
    # Cuando la clienta responde el formato compacto ("envío, Ana Pérez,
    # ana@email.com"), logística no forma parte de su nombre.
    name_text = re.sub(r"(?i)\b(env[ií]o|retiro|retirar)\b\s*", "", name_text)
    name_text = re.sub(r"[,:;|]+", " ", name_text).strip()
    name_words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", name_text)
    if len(name_words) < 2:
        return ()
    return " ".join(name_words[:5]), email_match.group(0).lower()


def _customer_details_prompt(include_quantity: bool = False) -> str:
    """Request checkout data in a small, copyable WhatsApp form."""
    quantity_line = "Cantidad: \n" if include_quantity else ""
    return (
        "¡Buenísimo! Para dejarlo listo, copiá y completá estas líneas:\n"
        "{}"
        "Entrega: envío o retiro\n"
        "Nombre y apellido: \n"
        "Email: "
    ).format(quantity_line)


def _sales_fulfillment(text: str) -> str:
    normalized = _normalized_text(text)
    if any(word in normalized for word in ("retiro", "retirar", "showroom", "paso a buscar")):
        return "pickup"
    if any(word in normalized for word in ("envio", "enviar", "domicilio", "correo")):
        return "shipping"
    return ""


def _looks_like_new_customer_request(text: str) -> bool:
    """Do not trap a new question inside an old confirmation screen."""
    normalized = _normalized_text(text).strip()
    return bool(
        re.match(r"^(hola|buenas|buen dia|buenas tardes)\b", normalized)
        or re.search(r"\b(busco|quisiera saber|tienen|tenes|me recomendas)\b", normalized)
    )


def _sales_summary(intake: dict) -> str:
    fulfillment = "envío" if intake["fulfillment"] == "shipping" else "retiro"
    price_summary = ""
    if intake["unit_price"] is not None:
        try:
            subtotal = Decimal(str(intake["unit_price"])) * intake["quantity"]
            formatted_subtotal = _format_ars(subtotal)
            price_summary = (
                "Subtotal de productos: {}\n"
                "Envío: a confirmar\n"
                "Total final: a confirmar\n"
            ).format(formatted_subtotal)
        except (InvalidOperation, TypeError):
            pass

    return (
        "Te resumo antes de pasárselo a Isa:\n"
        "Producto/modelo: {}\n"
        "Variante: {}\n"
        "Cantidad: {}\n"
        "Entrega: {}\n"
        "Nombre: {}\n"
        "Email: {}\n\n"
        "{}\n"
        "¿Confirmás que lo preparemos para revisión?"
    ).format(
        intake["product_request"],
        intake["selected_variant"] or "a confirmar",
        intake["quantity"],
        fulfillment,
        intake["customer_name"],
        intake["customer_email"],
        price_summary,
    )


def _recent_candidate_quantity(prior_history: list, sale_candidate: dict) -> int:
    product_words = {
        word for word in _normalized_text(sale_candidate.get("product_name", "")).split()
        if len(word) >= 4
    }
    for item in reversed(prior_history[-6:]):
        if item.get("role") != "user":
            continue
        message = item.get("content", "")
        if product_words.intersection(_normalized_text(message).split()):
            quantity = _extract_quantity(message)
            if quantity:
                return quantity
    return 0


def _verified_purchase_candidate_from_tool_calls(message_text: str, result: dict) -> dict:
    """Recover a concrete sale choice when the model verified one SKU but omitted the marker.

    The language model is allowed to explain the product, but it must not be the
    source of truth for the sales-form state. When a customer explicitly names
    a purchase and the same turn checked exactly one SKU, re-check that SKU and
    start the deterministic intake form. More than one stock lookup means the
    choice is ambiguous, so we intentionally do nothing here.
    """
    normalized = _normalized_text(message_text)
    expresses_purchase = bool(
        re.search(
            r"\b(comprar|compra|pedir|pido|ordenar|llevar|llevo|avanzar|avancemos|proceder)\b",
            normalized,
        )
    )
    if not expresses_purchase:
        return {}

    checked_skus = [
        (call.get("arguments", {}).get("sku") or "").strip()
        for call in result.get("tool_calls", [])
        if call.get("name") == "get_stock"
    ]
    checked_skus = list(dict.fromkeys(sku for sku in checked_skus if sku))
    if len(checked_skus) != 1:
        return {}

    stock = get_stock(checked_skus[0])
    if stock.get("status") != "in_stock":
        return {}

    return {
        "sku": stock["sku"],
        "product_name": stock["product_name"],
        "variant": stock.get("variant") or "",
        "unit_price": stock.get("price"),
    }


def _already_asked_product_clarification(prior_history: list) -> bool:
    """Return True only after Fred already asked once to identify a model."""
    for item in reversed(prior_history[-4:]):
        if item.get("role") != "assistant":
            continue
        text = _normalized_text(item.get("content", ""))
        return "asegurarme de ubicar el modelo correcto" in text
    return False


def _start_sales_intake(
    conversation_id: int,
    sale_candidate: dict = None,
    quantity: int = 0,
) -> str:
    if sale_candidate:
        product_request = sale_candidate["product_name"]
        selected_variant = sale_candidate.get("variant") or ""
        start_sales_intake(
            conversation_id,
            product_request=product_request,
            selected_sku=sale_candidate["sku"],
            selected_variant=selected_variant,
            unit_price=sale_candidate.get("unit_price"),
            quantity=quantity or None,
        )
        if quantity:
            return _customer_details_prompt()
        return _customer_details_prompt(include_quantity=True)

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
            reply = (
                "Para confirmarlo bien me falta la cantidad. "
                + _customer_details_prompt(include_quantity=True)
            )
        else:
            set_sales_intake_quantity(conversation_id, quantity)
            fulfillment = _sales_fulfillment(message_text)
            customer_details = _extract_customer_details(message_text)
            if fulfillment:
                set_sales_intake_fulfillment(conversation_id, fulfillment)
            if customer_details:
                customer_name, customer_email = customer_details
                if fulfillment:
                    set_sales_intake_customer(conversation_id, customer_name, customer_email)
                    reply = _sales_summary(get_active_sales_intake(conversation_id))
                else:
                    reply = "Me falta confirmar si preferís envío o retiro."
            elif fulfillment:
                reply = "Perfecto. " + _customer_details_prompt()
            else:
                reply = "Me falta confirmar la entrega. " + _customer_details_prompt()
    elif intake["status"] == "fulfillment":
        fulfillment = _sales_fulfillment(message_text)
        if not fulfillment:
            reply = "Me falta confirmar si preferís envío o retiro."
        else:
            set_sales_intake_fulfillment(conversation_id, fulfillment)
            customer_details = _extract_customer_details(message_text)
            if customer_details:
                customer_name, customer_email = customer_details
                set_sales_intake_customer(conversation_id, customer_name, customer_email)
                reply = _sales_summary(get_active_sales_intake(conversation_id))
            else:
                reply = "Perfecto. " + _customer_details_prompt()
    elif intake["status"] == "customer":
        customer_details = _extract_customer_details(message_text)
        if not customer_details:
            reply = "Necesito nombre y apellido + un email válido.\n" + _customer_details_prompt()
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
                "unit_price": str(intake["unit_price"]) if intake["unit_price"] is not None else "a confirmar",
                "products_subtotal": (
                    str(Decimal(str(intake["unit_price"])) * intake["quantity"])
                    if intake["unit_price"] is not None
                    else "a confirmar"
                ),
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
        elif re.match(r"^(?:quiero\s+)?(?:cambiar|corregir)(?:lo)?\b", normalized):
            cancel_sales_intake(conversation_id)
            reply = (
                "Dale, descarté ese resumen para corregirlo. Decime qué producto "
                "y cantidad querés llevar, y lo armamos de nuevo 😊"
            )
        elif _looks_like_new_customer_request(message_text):
            # La persona arrancó otra consulta: no la obligamos a terminar un
            # borrador viejo. Al devolver False, el webhook la procesa con Fred.
            cancel_sales_intake(conversation_id)
            return False
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


def _handle_isa_reminder_request(message_text: str) -> bool:
    """Let Isa manage reminders in ordinary language, without a separate panel."""
    normalized = _normalized_text(message_text)
    now = datetime.now(ARGENTINA_TZ)

    if re.search(r"\b(no me recuerdes|silencia|silencia los recordatorios)\b", normalized):
        tomorrow = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        snooze_isa_reminders(ISA_WHATSAPP_NUMBER, tomorrow)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Dale, no te insisto más hoy. Si sigue pendiente, te lo recuerdo mañana a las 10 😊",
        )
        return True

    match = re.search(r"\brecordame en (\d{1,2})\s*(minuto|min|hora|horas|h)\b", normalized)
    if match:
        amount = int(match.group(1))
        minutes = amount if match.group(2).startswith("min") else amount * 60
        if not 10 <= minutes <= 12 * 60:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Puedo recordártelo entre 10 minutos y 12 horas. Por ejemplo: “recordame en 1 hora”.",
            )
            return True
        remind_at = now + timedelta(minutes=minutes)
        snooze_isa_reminders(ISA_WHATSAPP_NUMBER, remind_at)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Dale, te lo recuerdo a las {}. Mientras tanto no te molesto 😊".format(
                remind_at.strftime("%H:%M")
            ),
        )
        return True

    if re.search(r"\b(reactiva|volve a recordar|vuelve a recordar|recordame ahora)\b", normalized):
        clear_isa_reminder_snooze(ISA_WHATSAPP_NUMBER)
        snapshot = pending_reminder_snapshot()
        if snapshot["count"]:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Listo, vuelvo a avisarte. Tenés {} pendiente(s); escribime “ver” y te muestro el primero.".format(
                    snapshot["count"]
                ),
            )
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, no hay pendientes ahora 😊")
        return True

    return False


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


ISA_SALE_TYPE_LABELS = {
    "normal": "Venta normal",
    "encargo": "Encargo",
    "venta_mayorista": "Venta mayorista",
    "otro": "Otro",
}


def _looks_like_isa_sale_request(message_text: str) -> bool:
    """Understand natural internal requests without requiring a magic command."""
    normalized = _normalized_text(message_text)
    return bool(
        re.search(
            r"\b(vendi|venta|orden|link de pago|link|cobrar|registrar|pedido)\b",
            normalized,
        )
    )


def send_isa_sale_type_menu() -> bool:
    """Ask Isa to classify an external sale with a WhatsApp list, not syntax."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": (
                    "¿Cómo querés registrar esta venta? Elegí una opción y después "
                    "te pido los datos. Todavía no se crea ningún link."
                )
            },
            "action": {
                "button": "Elegir tipo",
                "sections": [
                    {
                        "title": "Tipo de venta",
                        "rows": [
                            {"id": "sale_type:normal", "title": "Venta normal", "description": "Producto con stock físico"},
                            {"id": "sale_type:encargo", "title": "Encargo", "description": "Producto a pedir / sin stock físico"},
                            {"id": "sale_type:venta_mayorista", "title": "Venta mayorista", "description": "Condición comercial especial"},
                            {"id": "sale_type:otro", "title": "Otro", "description": "Contame el caso y lo clasificamos"},
                        ],
                    }
                ],
            },
        },
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[Isa] Menú interno HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando menú interno a Isa: {type(error).__name__}")
        return False


def _isa_sale_type_prompt(sale_type: str) -> str:
    if sale_type == "otro":
        return (
            "Listo, marcamos ‘Otro’. Contame brevemente qué pasó y qué necesitás. "
            "No voy a crear nada hasta que el tipo de venta quede claro."
        )
    return (
        "Perfecto: {}. Ahora pasame en un solo mensaje producto, variante, cantidad "
        "y nombre/email de la clienta si lo tenés. Voy a armar un borrador para tu "
        "aprobación; todavía no se crea ningún link."
    ).format(ISA_SALE_TYPE_LABELS[sale_type])


def _handle_isa_sale_session(message_text: str, button_reply_id: str) -> bool:
    """Advance Isa's internal guided draft. Returns True when it handled input."""
    if button_reply_id.startswith("sale_type:"):
        sale_type = button_reply_id.split(":", 1)[1]
        if sale_type not in ISA_SALE_TYPE_LABELS:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No reconocí ese tipo. Elegí una opción de la lista.")
            return True
        set_isa_sale_session_type(ISA_WHATSAPP_NUMBER, sale_type)
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, _isa_sale_type_prompt(sale_type))
        return True

    # Approval/context buttons belong to customer pending actions, never to an
    # unfinished internal sale draft.
    if button_reply_id:
        return False

    session = get_isa_sale_session(ISA_WHATSAPP_NUMBER)
    if not session:
        return False

    normalized = _normalized_text(message_text).strip()
    if re.fullmatch(r"(?:cancelar borrador|descartar borrador|cancelar venta)", normalized):
        clear_isa_sale_session(ISA_WHATSAPP_NUMBER)
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, descarté ese borrador interno. No se creó nada.")
        return True

    # "Cancelar" used to silently discard Isa's own draft even when the visible
    # thing she meant was a customer's pending approval. Keep those actions
    # deliberately separate: only the card button can return a customer to Fred.
    if re.fullmatch(r"(?:cancelar|cancelalo|dejalo)", normalized):
        if pending_action_count():
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Veo una clienta pendiente. Para devolver ese chat a Fred, tocá “Descartar” "
                "en su tarjeta. Si querías cerrar solo tu borrador interno, escribí “cancelar borrador”.",
            )
        else:
            clear_isa_sale_session(ISA_WHATSAPP_NUMBER)
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, descarté ese borrador interno. No se creó nada.")
        return True

    if session["status"] == "choose_type":
        send_isa_sale_type_menu()
        return True

    if session["status"] == "collect_details":
        add_isa_sale_session_details(ISA_WHATSAPP_NUMBER, message_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Borrador de {} guardado ✅\n\n{}\n\n"
            "Todavía no se creó ninguna orden ni link. La próxima fase agrega la "
            "revisión y tu botón de aprobación."
            .format(ISA_SALE_TYPE_LABELS[session["sale_type"]], message_text[:600]),
        )
        return True

    if session["status"] == "review":
        if _looks_like_isa_sale_request(message_text):
            start_isa_sale_session(ISA_WHATSAPP_NUMBER)
            send_isa_sale_type_menu()
            return True
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Ese borrador ya está guardado. Si querés cerrarlo, escribí “cancelar borrador”; "
            "o mandame una nueva venta para empezar otra ficha.",
        )
        return True

    return False


def _pending_action_by_id(action_id: int) -> dict:
    """Read one still-pending card; used before a demo-only side effect."""
    return next(
        (item for item in list_pending_actions(limit=20) if item["id"] == action_id),
        None,
    )


def _create_demo_link_for_approved_sale(action: dict) -> dict:
    """Create a checkout only in the dedicated demo store.

    The real product is intentionally never copied to the demo order. This
    verifies approval -> checkout plumbing with a clearly fake test SKU.
    """
    if not DEMO_APPROVALS_ENABLED:
        raise DraftOrderDemoError("La aprobación demo está apagada.")
    if action["action_type"] != "purchase_review":
        raise DraftOrderDemoError("Solo las fichas de compra pueden crear un link demo.")

    sale_draft = action.get("payload", {}).get("sale_draft", {})
    try:
        quantity = int(sale_draft.get("items_status", "").split("×", 1)[0].strip())
    except (ValueError, AttributeError):
        raise DraftOrderDemoError("No pude leer la cantidad de la ficha.")

    return create_demo_draft_order("TEST-FRED-001", quantity)


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

    if _handle_isa_reminder_request(message_text):
        return

    response_match = re.match(r"^reply_to_fred:(\d+)$", button_reply_id or "")
    if response_match:
        action_id = int(response_match.group(1))
        pending_action = _pending_action_by_id(action_id)
        if not pending_action or pending_action["action_type"] not in (
            "bot_fallback",
            "human_handoff",
        ):
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Esa consulta ya no está disponible.")
            return
        if wait_for_isa_response(action_id):
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Perfecto. Escribime la respuesta o dato que querés que Fred le comunique a la clienta. "
                "Fred se la envía y después retoma el chat.",
            )
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No pude preparar esa consulta para responder ahora.")
        return

    # Isa's text is intentionally handled before her own internal-sale draft:
    # once she chose “Responder a Fred”, her next message belongs to the
    # customer consultation and is delivered verbatim as reviewed information.
    awaiting_response = next(
        (
            action
            for action in list_pending_actions(limit=20)
            if action["action_type"] in ("bot_fallback", "human_handoff")
            and action.get("payload", {}).get("awaiting_isa_response")
        ),
        None,
    )
    if awaiting_response and not button_reply_id:
        normalized = _normalized_text(message_text).strip()
        if re.fullmatch(r"(?:cancelar|cancelalo|dejalo)", normalized):
            result = resolve_pending_action(awaiting_response["id"], "rejected")
            if result:
                set_conversation_state(result["conversation_id"], "BOT")
                send_whatsapp_text(
                    ISA_WHATSAPP_NUMBER,
                    "Dale, no envié ninguna respuesta de Isa. Fred vuelve a atender a la clienta.",
                )
            return

        customer_phone = awaiting_response["customer_phone"]
        if not send_whatsapp_text(customer_phone, message_text):
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude enviar esa respuesta a la clienta; no la pierdo. Probá mandarla de nuevo.",
            )
            return
        result = resolve_pending_action(awaiting_response["id"], "approved")
        if not result:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "La respuesta llegó a la clienta, pero el pendiente cambió de estado. Revisalo antes de seguir.",
            )
            return
        set_conversation_state(result["conversation_id"], "BOT")
        record_bot_message(result["conversation_id"], message_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, Fred le pasó tu respuesta a la clienta y vuelve a atender ese chat 😊",
        )
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if _handle_isa_sale_session(message_text, button_reply_id):
        return

    if _looks_like_isa_sale_request(message_text):
        start_isa_sale_session(ISA_WHATSAPP_NUMBER)
        send_isa_sale_type_menu()
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

    match = re.match(
        r"^(approve|approve_demo|approve_checkout|reject|view|take_handoff|pause_bot|resume_bot):(\d+)$",
        button_reply_id or "",
    )
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

    if action == "approve_demo":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        try:
            draft_order = _create_demo_link_for_approved_sale(pending_action)
        except DraftOrderDemoError as error:
            # La tarjeta queda pendiente para que Isa pueda corregir o descartar.
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No creé ningún link demo. {}".format(error),
            )
            return

        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "El link demo se creó, pero el pendiente ya había sido resuelto. No se envió a la clienta.",
            )
            return

        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Link demo creado tras tu aprobación ✅\nBorrador #{}\n{}\n\n"
            "Usa TEST-FRED-001 y no corresponde al producto real ni se envía a la clienta."
            .format(draft_order["id"], draft_order["checkout_url"]),
        )
        print("[Isa] Borrador demo #{} creado desde pendiente #{}.".format(draft_order["id"], action_id))
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action in ("take_handoff", "pause_bot"):
        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
            return
        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, Fred queda en pausa para esta clienta. En el número de prueba todavía no hay una bandeja "
            "compartida para que respondas como Isa; esa capa llega con el número oficial y coexistencia.",
        )
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action == "resume_bot":
        result = resolve_pending_action(action_id, "rejected")
        if not result:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
            return
        set_conversation_state(result["conversation_id"], "BOT")
        customer_text = "Dale, sigo por acá 😊 ¿Qué te gustaría resolver?"
        if send_whatsapp_text(result["payload"].get("customer_phone", ""), customer_text):
            record_bot_message(result["conversation_id"], customer_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, Fred retoma esa conversación.",
        )
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action == "approve_checkout":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        if pending_action["action_type"] != "purchase_review":
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente no es una compra para aprobar.")
            return

        payload = pending_action.get("payload", {})
        sale_draft = payload.get("sale_draft", {})
        checkout = payload.get("checkout")
        try:
            if not checkout:
                items_status = sale_draft.get("items_status", "")
                quantity = int(items_status.split("×", 1)[0].strip())
                checkout = create_approved_checkout(
                    sku=sale_draft.get("selected_sku", ""),
                    quantity=quantity,
                    customer_name=sale_draft.get("customer_name", ""),
                    customer_email=sale_draft.get("customer_email", ""),
                    customer_phone=pending_action["customer_phone"],
                )
                save_pending_action_checkout(action_id, checkout)
        except (CheckoutError, ValueError, IndexError) as error:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No se creó ningún link. {} El pendiente sigue abierto para revisarlo.".format(error),
            )
            return

        customer_text = (
            "¡Listo! Isa revisó tu pedido 😊\n\n"
            "Te dejo el link seguro para completar la compra:\n{}\n\n"
            "Ahí vas a poder ingresar la dirección si elegís envío, o seleccionar retiro, "
            "y elegir el medio de pago. Cuando se acredite, Tiendanube registra el pedido."
        ).format(checkout["checkout_url"])
        if not send_whatsapp_text(pending_action["customer_phone"], customer_text):
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "El checkout #{} ya está creado, pero no pude entregar el link. Tocá "
                "“Aprobar compra” otra vez: se reutiliza el mismo link.".format(checkout.get("id", "")),
            )
            return

        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "El link ya fue enviado a la clienta, pero el pendiente cambió de estado. Revisalo en Tiendanube.",
            )
            return
        set_conversation_state(result["conversation_id"], "BOT")
        record_bot_message(result["conversation_id"], customer_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Compra aprobada ✅ Link de checkout #{} enviado a la clienta. Tiendanube va a registrar "
            "el pago y el pedido cuando ella complete el checkout.".format(checkout.get("id", "")),
        )
        print("[Isa] Checkout #{} enviado desde pendiente #{}.".format(checkout.get("id"), action_id))
        if pending_action_count():
            send_next_pending_to_isa()
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
            "Cancelaste el pendiente #{}. Fred vuelve a atender a la clienta."
            .format(action_id),
        )

    if pending_action_count():
        send_next_pending_to_isa()


# ============================================================
# WEBHOOK — VERIFICACIÓN META
# ============================================================


# ============================================================
# TIENDANUBE — CONEXIÓN OAUTH ASISTIDA
# ============================================================

def _oauth_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Render a small, non-cacheable status page without exposing credentials."""

    document = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:48px auto;line-height:1.5;padding:0 20px">
<h1>{title}</h1>{body}</body></html>""".format(
        title=html.escape(title), body=body
    )
    return HTMLResponse(
        content=document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/tiendanube/connect")
async def tiendanube_connect():
    """Start a safe owner-authorized Tiendanube OAuth connection."""

    client_id = os.getenv("TIENDANUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    # The generic Tiendanube authorization URL can reuse the Partner demo-store
    # session. Start from the real store's admin domain instead, so the owner
    # authorizes the intended store while the state cookie still protects the
    # callback.
    store_domain = os.getenv(
        "TIENDANUBE_STORE_DOMAIN", "beautyhouse5.mitiendanube.com"
    ).strip().lower()
    if not client_id or not client_secret:
        return _oauth_page(
            "Conexión no configurada",
            "<p>Faltan las credenciales de la app de Tiendanube en Railway.</p>",
            503,
        )
    if not re.fullmatch(r"[a-z0-9-]+\.mitiendanube\.com", store_domain):
        return _oauth_page(
            "Dominio de tienda inválido",
            "<p>Revisá <code>TIENDANUBE_STORE_DOMAIN</code> en Railway.</p>",
            503,
        )

    state = secrets.token_urlsafe(32)
    response = RedirectResponse(
        "https://{}/admin/apps/{}/authorize?state={}".format(
            store_domain, client_id, state
        ),
        status_code=302,
    )
    response.set_cookie(
        "fred_tiendanube_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/tiendanube/oauth/callback")
async def tiendanube_oauth_callback(request: Request):
    """Exchange an authorization code and store the token encrypted."""

    client_id = os.getenv("TIENDANUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    expected_state = request.cookies.get("fred_tiendanube_oauth_state", "")
    if not client_id or not client_secret:
        return _oauth_page("Conexión no configurada", "<p>Faltan credenciales de la app en Railway.</p>", 503)
    if not code or not expected_state or not secrets.compare_digest(state, expected_state):
        return _oauth_page(
            "No se pudo verificar la conexión",
            "<p>Volvé a iniciar desde el link de conexión de Fred.</p>",
            400,
        )

    try:
        token_response = requests.post(
            "https://www.tiendanube.com/apps/authorize/token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=15,
        )
        payload = token_response.json()
    except requests.RequestException:
        print("[Tiendanube OAuth] No se pudo contactar el endpoint de autorización.")
        return _oauth_page(
            "No se pudo conectar Tiendanube",
            "<p>No se pudo contactar Tiendanube. Probá nuevamente en unos minutos.</p>",
            502,
        )
    except ValueError:
        print("[Tiendanube OAuth] Respuesta inválida del canje.")
        return _oauth_page(
            "Respuesta inválida de Tiendanube",
            "<p>Volvé a iniciar desde el link de conexión de Fred.</p>",
            400,
        )

    if not token_response.ok:
        print(
            "[Tiendanube OAuth] Canje rechazado "
            "(HTTP {}).".format(token_response.status_code)
        )
        return _oauth_page(
            "Tiendanube rechazó la conexión",
            "<p>Revisá en Railway que <code>TIENDANUBE_CLIENT_ID</code> sea 38765 "
            "y que <code>TIENDANUBE_CLIENT_SECRET</code> corresponda a esta misma app.</p>",
            400,
        )

    try:
        store_id = str(payload.get("user_id", "")).strip()
        access_token = str(payload.get("access_token", "")).strip()
        expected_store_id = os.getenv("TIENDANUBE_STORE_ID", "").strip()
        if store_id != expected_store_id:
            print(
                "[Tiendanube OAuth] Tienda inesperada: {}.".format(store_id)
            )
            return _oauth_page(
                "Se conectó otra tienda",
                "<p>Tiendanube devolvió la tienda <strong>{}</strong>, pero Fred espera "
                "Beauty House (<strong>{}</strong>). No se modificó ninguna credencial.</p>".format(
                    html.escape(store_id or "sin identificador"),
                    html.escape(expected_store_id or "sin configurar"),
                ),
                400,
            )
        save_tiendanube_credential(
            store_id,
            access_token,
            str(payload.get("scope", "")),
        )
    except TiendanubeCredentialError:
        print("[Tiendanube OAuth] La credencial no pudo guardarse de forma segura.")
        return _oauth_page(
            "No se pudo guardar la conexión",
            "<p>Revisá las variables de Tiendanube y Supabase en Railway.</p>",
            400,
        )
    except psycopg2.Error:
        print("[Tiendanube OAuth] Supabase rechazó el guardado de la conexión.")
        return _oauth_page(
            "No se pudo guardar la conexión",
            "<p>La autorización fue válida, pero Fred no pudo guardarla en Supabase. "
            "Revisemos la conexión de Supabase.</p>",
            503,
        )

    response = _oauth_page(
        "Beauty House conectada",
        "<p>La autorización quedó guardada de forma cifrada y Fred ya puede usarla.</p>"
        "<p><strong>Tienda verificada:</strong> {}</p>".format(html.escape(store_id)),
    )
    response.delete_cookie("fred_tiendanube_oauth_state")
    return response

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
    interactive_reply = msg.get("interactive", {})
    button_reply_id = (
        interactive_reply.get("button_reply", {}).get("id", "")
        or interactive_reply.get("list_reply", {}).get("id", "")
    )

    message_text = (
        msg.get("text", {})
        .get("body", "")
        .strip()
    )

    if not message_text and button_reply_id:
        message_text = (
            interactive_reply.get("button_reply", {}).get("title", "")
            or interactive_reply.get("list_reply", {}).get("title", "")
        ).strip()

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
            # Un fallo de identificación recibe una sola repregunta. Si la
            # clienta ya respondió esa repregunta y aún no podemos verificar el
            # modelo, Isa recibe un caso realmente excepcional y con contexto.
            if (
                result.get("needs_product_clarification")
                and not handoff
                and _already_asked_product_clarification(prior_history)
            ):
                handoff = {
                    "reason": "unable_to_verify",
                    "summary": (
                        "Fred pidió una precisión para identificar el producto, "
                        "pero todavía no pudo verificarlo en Tiendanube."
                    ),
                }
            # DeepSeek can sometimes explain a purchase correctly but omit the
            # select_sale_candidate call. Do not let that create a fake
            # text-only checkout flow: if it verified exactly one SKU in this
            # turn, promote it to the persisted intake form ourselves.
            if not sale_candidate and SALES_INTAKE_ENABLED:
                sale_candidate = _verified_purchase_candidate_from_tool_calls(
                    message_text,
                    result,
                )
            if sale_candidate and SALES_INTAKE_ENABLED:
                try:
                    candidate_quantity = (
                        _extract_quantity(message_text)
                        or _recent_candidate_quantity(prior_history, sale_candidate)
                    )
                    reply = _start_sales_intake(
                        conversation_id,
                        sale_candidate,
                        quantity=candidate_quantity,
                    )
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
