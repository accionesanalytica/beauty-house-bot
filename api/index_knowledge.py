"""Indexes curated Markdown knowledge into Supabase pgvector.

The default is a cost-free dry run. ``--apply`` calls Gemini embeddings and
writes only the reviewed Markdown sources under ``knowledge/``. Never place
chat exports, credentials, payment details or unreviewed historical material
in that folder.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import psycopg2
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
sys.path.insert(0, str(PROJECT_DIR / "bot"))
from knowledge_rag import (  # noqa: E402
    canonical_knowledge_embedding_text,
    load_knowledge_chunks,
)

DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
BATCH_SLEEP = 0.1
KNOWLEDGE_V1_SOURCE_IDS = {
    "facts-commercial-operations-v1",
    "facts-lashes-products-v1",
    "facts-order-tracking-v1",
    "facts-pickups-showroom-v1",
    "facts-touchup-kit-v1",
    "faq-customer-service-v1",
    "procedure-commercial-postsale-v1",
    "procedure-lashes-guidance-v1",
    "procedure-order-tracking-v1",
    "procedure-pickups-v1",
    "procedure-touchup-kit-spray-v1",
}


def embed(client, text):
    result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIMS,
            task_type="RETRIEVAL_DOCUMENT",
        ),
    )
    vector = np.array(result.embeddings[0].values)
    return (vector / np.linalg.norm(vector)).tolist()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    chunks = load_knowledge_chunks(KNOWLEDGE_DIR)
    source_ids = {chunk.source_id for chunk in chunks}
    print("Fuentes curadas: {}".format(len(list(KNOWLEDGE_DIR.rglob("*.md")))))
    print("Fuentes indexables: {}".format(len(source_ids)))
    print("Chunks a indexar: {}".format(len(chunks)))
    for chunk in chunks[:5]:
        print("  [{} / {}] {}".format(chunk.source_id, chunk.section, chunk.content[:90]))

    if not args.apply:
        print("\nSimulación sin costo. Agregá --apply para generar embeddings y guardar cambios.")
        return
    if not DB_URL or not GEMINI_KEY:
        print("Falta SUPABASE_DB_URL o GEMINI_API_KEY en el entorno.")
        return
    if source_ids != KNOWLEDGE_V1_SOURCE_IDS or len(chunks) != 42:
        print(
            "La selección no coincide con Knowledge V1: {} fuentes y {} chunks. "
            "No se escribió nada.".format(len(source_ids), len(chunks))
        )
        return

    client = genai.Client(api_key=GEMINI_KEY)
    prepared = []
    for chunk in chunks:
        try:
            vector = embed(client, canonical_knowledge_embedding_text(chunk))
            prepared.append((chunk, vector))
        except Exception as error:  # noqa: BLE001
            print("  ERROR [{}]: {}".format(chunk.source_id, type(error).__name__))
            print("Falló la preparación. No se escribió nada.")
            return
        time.sleep(BATCH_SLEEP)

    connection = psycopg2.connect(DB_URL)
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'knowledge_chunks' "
            "AND column_name = 'metadata')"
        )
        if not cursor.fetchone()[0]:
            raise RuntimeError(
                "Falta aplicar docs/sql/007_knowledge_metadata.sql."
            )

        # Reemplazo atómico y limitado: ninguna fuente histórica o comercial
        # fuera de Knowledge V1 cambia de estado ni se elimina.
        cursor.execute(
            "DELETE FROM knowledge_chunks WHERE source_id = ANY(%s)",
            (sorted(KNOWLEDGE_V1_SOURCE_IDS),),
        )
        for chunk, vector in prepared:
            cursor.execute(
                """
                INSERT INTO knowledge_chunks
                    (source_id, section, content, metadata, embedding, status,
                     active, reviewed_at, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, %s::vector,
                        'approved', true, now(), now())
                """,
                (
                    chunk.source_id,
                    chunk.section,
                    chunk.content,
                    json.dumps(dict(chunk.metadata), ensure_ascii=False),
                    str(vector),
                ),
            )
        connection.commit()
    except Exception as error:  # noqa: BLE001
        connection.rollback()
        print("Indexación revertida completamente: {}".format(type(error).__name__))
        raise
    finally:
        cursor.close()
        connection.close()
    print("Listo. {} chunks indexados en una sola transacción.".format(len(prepared)))


if __name__ == "__main__":
    main()
