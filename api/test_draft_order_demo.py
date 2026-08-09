#!/usr/bin/env python3
"""Validate or create one explicit draft order in Fred's demo store.

Usage:
    python api/test_draft_order_demo.py            # validates only
    python api/test_draft_order_demo.py --create   # creates one demo draft
"""

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "bot"))

from tiendanube_draft_orders import (  # noqa: E402
    DraftOrderDemoError,
    _configuration,
    _find_published_variant,
    create_demo_draft_order,
)


def load_local_dotenv() -> None:
    """Load simple KEY=value lines without requiring an activated venv."""
    dotenv_path = PROJECT_ROOT / ".env"
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true")
    args = parser.parse_args()
    load_local_dotenv()

    try:
        configuration = _configuration(require_enabled=False)
        variant = _find_published_variant(configuration, "TEST-FRED-001")
        print(
            "Demo validada: {} | variante {} | stock {} | precio {}".format(
                variant["product_name"],
                variant["variant_id"],
                variant["stock"],
                variant["price"],
            )
        )
        if not args.create:
            print("No se creó ninguna orden. Usá --create para hacer la prueba.")
            return

        draft_order = create_demo_draft_order(
            "TEST-FRED-001", 1, require_enabled=False
        )
        print("Orden borrador demo creada: {}".format(draft_order["id"]))
        print("Link de checkout: {}".format(draft_order["checkout_url"]))
        print("Estado de pago: {}".format(draft_order["payment_status"]))
    except DraftOrderDemoError as error:
        print("ERROR: {}".format(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
