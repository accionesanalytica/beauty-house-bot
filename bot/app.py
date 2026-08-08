"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, busca contexto en Supabase (RAG vectorial),
y responde usando el agent de DeepSeek.
"""

import json
import os
import sys
from typing import Any, Dict, Optional

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

# TODO: Importar agent (requiere arreglar OpenAI SDK conflict)
# from agent import answer

load_dotenv()

app = FastAPI()

# Configuración
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768

gemini_client = genai.Client(api_key=GEMINI_KEY)


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
    normalized = (vector / np.linalg.norm(vector)).tolist()
    return normalized


def search_similar_products(query: str, limit: int = 3) -> str:
    """
    Busca productos similares en Supabase usando búsqueda vectorial.
    Devuelve un string con el contexto para el agent.
    """
    try:
        embedding = embed_text(query, task_type="RETRIEVAL_QUERY")

        conn = psycopg2.connect(SUPABASE_DB_URL)
        cursor = conn.cursor()

        # Búsqueda por similitud (cosine distance)
        cursor.execute("""
            SELECT
                product_id, variant_id, sku, product_name, variant,
                1 - (embedding <=> %s::vector) AS similarity
            FROM product_embeddings
            WHERE published = true
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, (str(embedding), str(embedding), limit))

        results = cursor.fetchall()
        cursor.close()
        conn.close()

        if not results:
            return ""

        context = "Productos encontrados:\n"
        for row in results:
            product_id, variant_id, sku, product_name, variant, similarity = row
            context += (
                f"- {product_name} (SKU: {sku or 'N/A'}) "
                f"Variante: {variant or 'default'} "
                f"(similitud: {similarity:.2f})\n"
            )

        return context

    except Exception as e:
        print(f"ERROR en search_similar_products: {e}")
        return ""


def send_whatsapp_message(phone_number: str, message: str) -> bool:
    """Envía un mensaje de vuelta a WhatsApp."""
    url = f"https://graph.instagram.com/v18.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"ERROR enviando mensaje a WhatsApp: {e}")
        return False


@app.get("/webhook")
async def webhook_get(request: Request):
    """Verificación del webhook por parte de Meta."""
    verify_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if verify_token == WHATSAPP_WEBHOOK_VERIFY_TOKEN:
        return JSONResponse(content=int(challenge))

    return JSONResponse(content={"error": "Invalid token"}, status_code=403)


@app.post("/webhook")
async def webhook_post(request: Request):
    """Procesa mensajes entrantes de WhatsApp."""
    body = await request.json()

    # Meta envía un array de entries
    if "entry" not in body or not body["entry"]:
        return JSONResponse(content={"ok": True})

    entry = body["entry"][0]

    # Verificar si hay cambios en messaging
    if "changes" not in entry or not entry["changes"]:
        return JSONResponse(content={"ok": True})

    change = entry["changes"][0]

    # Verificar si hay mensajes
    if "value" not in change or "messages" not in change["value"]:
        return JSONResponse(content={"ok": True})

    messages = change["value"]["messages"]

    if not messages:
        return JSONResponse(content={"ok": True})

    msg = messages[0]
    customer_phone = msg.get("from")
    message_text = msg.get("text", {}).get("body", "").strip()

    if not message_text:
        return JSONResponse(content={"ok": True})

    print(f"\n[WhatsApp] {customer_phone}: {message_text}")

    # TODO: Implementar agent loop con DeepSeek API directo (sin OpenAI SDK)
    # Por ahora, respuesta temporal
    reply = "Hola! El bot está en construcción. Pronto podré ayudarte con consultas de stock 😊"

    # Responder a WhatsApp
    send_whatsapp_message(customer_phone, reply)

    return JSONResponse(content={"ok": True})


@app.get("/health")
async def health():
    """Health check para Railway."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
