"""
Indexes the Tiendanube catalog into Supabase (pgvector).

Source of truth: data/catalog_flat.csv, produced by fetch_catalog.py.
Run fetch_catalog.py first so the catalog is fresh.

IMPORTANT DESIGN NOTE
    Stock is NOT part of the embedded text. Only product identity is
    embedded (name, variant, brand, sku). Stock changes many times a day;
    identity does not. The bot resolves the product semantically here,
    then calls the Tiendanube API for the live stock number.

Only published products are indexed: the bot must never offer something
that is not for sale. The published flag is persisted so stale variants can
be excluded after later catalog updates.

Usage:
    python api/index_catalog.py            # dry run, shows what it would do
    python api/index_catalog.py --apply
"""

import argparse
import csv
import os
import sys
import time

import numpy as np
import psycopg2
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768          # must match vector(768) in the table
BATCH_SLEEP = 0.1

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(SCRIPT_DIR, "..", "data", "catalog_flat.csv")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "..", "bot"))
from catalog_rag import build_catalog_content  # noqa: E402

gemini = genai.Client(api_key=GEMINI_KEY)


def embed(text, task_type="RETRIEVAL_DOCUMENT"):
    """Returns a normalized 768-dimension embedding."""
    result = gemini.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIMS,
            task_type=task_type,
        ),
    )
    vector = np.array(result.embeddings[0].values)
    return (vector / np.linalg.norm(vector)).tolist()


def build_content(row):
    """Compatibility wrapper used by the indexer and its dry-run preview."""
    return build_catalog_content(row)


def load_catalog():
    with open(CATALOG_PATH, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    # Only what is actually for sale
    return [r for r in rows if r["published"] == "yes"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not DB_URL:
        print("Falta SUPABASE_DB_URL en el .env")
        return
    if not GEMINI_KEY:
        print("Falta GEMINI_API_KEY en el .env")
        return

    rows = load_catalog()
    if args.limit:
        rows = rows[:args.limit]

    print("\nVariantes publicadas a indexar: {}".format(len(rows)))
    print("Modelo: {} ({} dimensiones)\n".format(EMBED_MODEL, EMBED_DIMS))

    for row in rows[:5]:
        print("  {}".format(build_content(row)[:88]))
    if len(rows) > 5:
        print("  ... y {} mas\n".format(len(rows) - 5))

    if not args.apply:
        print("Simulacion. Agrega --apply para generar los embeddings y guardarlos.")
        return

    connection = psycopg2.connect(DB_URL)
    cursor = connection.cursor()

    # Older versions of the table did not persist publication status. Add it
    # safely, then hide every old row until the current catalog reaffirms it.
    cursor.execute(
        "ALTER TABLE product_embeddings "
        "ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false"
    )
    cursor.execute("UPDATE product_embeddings SET published = false")
    connection.commit()

    done = failed = 0

    for index, row in enumerate(rows, start=1):
        content = build_content(row)

        try:
            vector = embed(content)

            cursor.execute(
                """
                insert into product_embeddings
                    (product_id, variant_id, sku, product_name, variant, content, embedding, published, updated_at)
                values (%s, %s, %s, %s, %s, %s, %s::vector, true, now())
                on conflict (variant_id) do update set
                    product_id   = excluded.product_id,
                    sku          = excluded.sku,
                    product_name = excluded.product_name,
                    variant      = excluded.variant,
                    content      = excluded.content,
                    embedding    = excluded.embedding,
                    published    = excluded.published,
                    updated_at   = now()
                """,
                (
                    int(row["product_id"]),
                    int(row["variant_id"]),
                    row["sku"].strip() or None,
                    row["product_name"],
                    row["variant_values"].strip() or None,
                    content,
                    str(vector),
                ),
            )
            done += 1

        except Exception as error:  # noqa: BLE001
            failed += 1
            print("  ERROR {}: {}".format(row["product_name"][:36], error))

        if index % 50 == 0:
            connection.commit()
            print("  {}/{}".format(index, len(rows)))

        time.sleep(BATCH_SLEEP)

    connection.commit()
    cursor.close()
    connection.close()

    print("\nListo. {} indexados, {} con error.".format(done, failed))


if __name__ == "__main__":
    main()
