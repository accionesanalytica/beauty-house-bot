"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, registra su historial en Supabase y, por ahora,
responde con una plantilla de prueba aprobada por Meta.
"""

import os
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

from conversation_store import record_inbound_message

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
                f"- {product_name} "
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

    message_text = (
        msg.get("text", {})
        .get("body", "")
        .strip()
    )

    if not message_text:

        return JSONResponse(
            content={"ok": True}
        )

    print(
        f"\n[WhatsApp] "
        f"{customer_phone}: "
        f"{message_text}"
    )

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
        print(f"ERROR guardando conversacion (tipo: {type(error).__name__})")

    # ========================================================
    # RESPUESTA TEMPORAL — MODO SEGURO
    # ========================================================
    #
    # El historial ya queda guardado, pero todavía no se llama al agente.
    # BOT_RESPONSE_MODE se mantiene en "template" hasta completar una prueba
    # controlada del flujo conversacional.
    #

    # Enviar la plantilla Meta escalacion_isa con {{1}} = 1.
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
