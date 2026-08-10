"""Indexes curated Markdown knowledge into Supabase pgvector.

The default is a cost-free dry run. ``--apply`` calls Gemini embeddings and
writes only the reviewed Markdown sources under ``knowledge/``. Never place
chat exports, credentials, payment details or unreviewed historical material
in that folder.
"""

import argparse
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
from knowledge_rag import load_knowledge_chunks  # noqa: E402

DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
BATCH_SLEEP = 0.1


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
    print("Fuentes curadas: {}".format(len(list(KNOWLEDGE_DIR.glob("*.md")))))
    print("Chunks a indexar: {}".format(len(chunks)))
    for chunk in chunks[:5]:
        print("  [{} / {}] {}".format(chunk.source_id, chunk.section, chunk.content[:90]))

    if not args.apply:
        print("\nSimulación sin costo. Agregá --apply para generar embeddings y guardar cambios.")
        return
    if not DB_URL or not GEMINI_KEY:
        print("Falta SUPABASE_DB_URL o GEMINI_API_KEY en el entorno.")
        return

    client = genai.Client(api_key=GEMINI_KEY)
    connection = psycopg2.connect(DB_URL)
    cursor = connection.cursor()
    cursor.execute("UPDATE knowledge_chunks SET active = false")

    done = failed = 0
    for index, chunk in enumerate(chunks, start=1):
        try:
            vector = embed(client, chunk.content)
            cursor.execute(
                """
                INSERT INTO knowledge_chunks
                    (source_id, section, content, embedding, status, active, reviewed_at, updated_at)
                VALUES (%s, %s, %s, %s::vector, 'approved', true, now(), now())
                """,
                (chunk.source_id, chunk.section, chunk.content, str(vector)),
            )
            done += 1
        except Exception as error:  # noqa: BLE001
            failed += 1
            print("  ERROR [{}]: {}".format(chunk.source_id, type(error).__name__))
        if index % 25 == 0:
            connection.commit()
        time.sleep(BATCH_SLEEP)

    connection.commit()
    cursor.close()
    connection.close()
    print("Listo. {} chunks indexados, {} con error.".format(done, failed))


if __name__ == "__main__":
    main()
