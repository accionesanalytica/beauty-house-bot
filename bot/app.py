"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, busca contexto en Supabase (RAG vectorial),
y responde usando el agent de DeepSeek.
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

# TODO: Importar agent cuando esté listo
# from agent import answer

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

    except Exception as e:

        print(
            f"ERROR en search_similar_products: {e}"
        )

        return ""


# ============================================================
# WHATSAPP — ENVÍO DE MENSAJES
# ============================================================

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

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_number,
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

    # ========================================================
    # RESPUESTA TEMPORAL
    # ========================================================
    #
    # TODO:
    # Implementar agent loop con DeepSeek + RAG.
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
