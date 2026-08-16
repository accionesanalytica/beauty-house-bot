"""Regenerate data/product_lexicon.txt — the words that identify a product.

WHY THIS FILE EXISTS
--------------------
Fred must be able to tell that a customer NAMED a product ("Foxy Cat eye?"),
because naming one is a hard blocker against answering from an approved
document. That check needs real product names.

It used to read data/catalog.json, a 5.9 MB dump that .gitignore excludes as a
locally generated artifact. It therefore existed on one developer's machine
and nowhere else: CI and production both built an EMPTY lexicon and the guard
silently did nothing. The identity of the store's products is not a developer
artifact -- it belongs to the repository.

So the derived thing is versioned instead of the dump: about a thousand words,
a few kilobytes, identical in local, CI and production. Regenerate it whenever
the catalog gains a new product family:

    python api/build_product_lexicon.py            # from the live store
    python api/build_product_lexicon.py --snapshot # from data/catalog.json

Product FAMILIES change slowly; stock and price are not in here and never will
be -- those stay live, per request.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bot"))

from routing_policy import build_product_lexicon  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "data" / "product_lexicon.txt"
SNAPSHOT_PATH = REPO_ROOT / "data" / "catalog.json"


def names_from_snapshot():
    payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    products = payload if isinstance(payload, list) else payload.get("products", [])
    names = []
    for product in products:
        name = product.get("name") if isinstance(product, dict) else product
        if isinstance(name, dict):
            name = name.get("es") or name.get("pt") or ""
        if name:
            names.append(str(name))
    return names, "data/catalog.json"


def names_from_live_store():
    from tiendanube_tools import _get

    names = []
    for page in range(1, 31):
        products = _get("/products", {"page": page, "per_page": 200})
        if not products:
            break
        for product in products:
            name = product.get("name")
            if isinstance(name, dict):
                name = name.get("es") or name.get("pt") or ""
            if name:
                names.append(str(name))
    return names, "Tiendanube (live)"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot", action="store_true",
        help="usar data/catalog.json en lugar de la tienda en vivo",
    )
    arguments = parser.parse_args()

    if arguments.snapshot:
        names, source = names_from_snapshot()
    else:
        names, source = names_from_live_store()

    lexicon = sorted(build_product_lexicon(names))
    if not lexicon:
        raise SystemExit(
            "No se extrajo ninguna palabra identificadora de {} productos. "
            "No se escribe un léxico vacío: dejaría el guard de productos "
            "apagado sin que se note.".format(len(names))
        )

    OUTPUT_PATH.write_text(
        "\n".join(
            [
                "# Palabras que identifican un producto del catálogo real.",
                "# Generado por api/build_product_lexicon.py -- no editar a mano.",
                "# fuente: {} | productos: {} | fecha: {}".format(
                    source, len(names), date.today().isoformat()
                ),
            ]
            + lexicon
        )
        + "\n",
        encoding="utf-8",
    )
    print("{} palabras desde {} productos ({}) -> {}".format(
        len(lexicon), len(names), source, OUTPUT_PATH.relative_to(REPO_ROOT)))


if __name__ == "__main__":
    main()
