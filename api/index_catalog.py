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
import html
import json
import os
import re
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
EMBED_BATCH_SIZE = 50

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(SCRIPT_DIR, "..", "data", "catalog_flat.csv")
CATALOG_DETAILS_PATH = os.path.join(SCRIPT_DIR, "..", "data", "catalog.json")
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


def embed_batch(texts, task_type="RETRIEVAL_DOCUMENT"):
    """Embed a bounded batch so a catalog refresh is not easy to interrupt."""
    result = gemini.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIMS,
            task_type=task_type,
        ),
    )
    return [
        (np.array(item.values) / np.linalg.norm(np.array(item.values))).tolist()
        for item in result.embeddings
    ]


def build_content(row):
    """Compatibility wrapper used by the indexer and its dry-run preview."""
    return build_catalog_content(row)


def _plain_text(value):
    """Turn Tiendanube HTML into compact retrieval text, never customer HTML."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _catalog_details_by_product_id():
    """Optional rich source from the same Tiendanube export as catalog_flat.

    The flat file is enough for exact product identity.  When the detailed
    export exists, descriptions, brand and public handle make semantic product
    discovery understand needs such as "natural para todos los días".  Stock
    and price remain deliberately excluded.
    """
    if not os.path.exists(CATALOG_DETAILS_PATH):
        return {}
    with open(CATALOG_DETAILS_PATH, encoding="utf-8") as handle:
        products = json.load(handle)
    details = {}
    for product in products:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        description = _plain_text((product.get("description") or {}).get("es"))
        handle_value = str((product.get("handle") or {}).get("es") or "").strip()
        details[product_id] = {
            "description": description,
            "brand": str(product.get("brand") or "").strip(),
            "handle": handle_value,
        }
    return details


def load_catalog():
    with open(CATALOG_PATH, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))

    details_by_id = _catalog_details_by_product_id()
    for row in rows:
        row.update(details_by_id.get(str(row.get("product_id") or ""), {}))

    # Only what is actually for sale
    return [r for r in rows if r["published"] == "yes"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=int, default=0, help="0-based offset for resumable batches")
    args = parser.parse_args()

    if not DB_URL:
        print("Falta SUPABASE_DB_URL en el .env")
        return
    if not GEMINI_KEY:
        print("Falta GEMINI_API_KEY en el .env")
        return

    rows = load_catalog()
    if args.start:
        rows = rows[args.start:]
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
    # safely, but do not hide the prior index before the new run succeeds.
    cursor.execute(
        "ALTER TABLE product_embeddings "
        "ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false"
    )
    connection.commit()

    done = failed = 0

    for batch_start in range(0, len(rows), EMBED_BATCH_SIZE):
        batch_rows = rows[batch_start:batch_start + EMBED_BATCH_SIZE]
        contents = [build_content(row) for row in batch_rows]
        try:
            vectors = embed_batch(contents)
            if len(vectors) != len(batch_rows):
                raise RuntimeError("Gemini devolvió una cantidad incompleta de embeddings")
        except Exception as error:  # noqa: BLE001
            failed += len(batch_rows)
            print("  ERROR lote {}/{}: {}".format(batch_start + 1, len(rows), error))
            continue

        for row, content, vector in zip(batch_rows, contents, vectors):
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
        done += len(batch_rows)
        connection.commit()
        print("  {}/{}".format(batch_start + len(batch_rows), len(rows)))

        time.sleep(BATCH_SLEEP)

    # Only after a complete refresh do we hide variants missing from the
    # current source catalog. A partial run keeps the previous index usable.
    if failed == 0 and not args.limit and not args.start:
        cursor.execute(
            "UPDATE product_embeddings SET published = false "
            "WHERE NOT (variant_id = ANY(%s))",
            ([int(row["variant_id"]) for row in rows],),
        )
        connection.commit()
    elif failed:
        print("Indice previo conservado: hubo lotes con error.")
    cursor.close()
    connection.close()

    print("\nListo. {} indexados, {} con error.".format(done, failed))


if __name__ == "__main__":
    main()
