"""Pure, offline checks for catalog retrieval quality boundaries."""

import sys
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from catalog_rag import (  # noqa: E402
    build_catalog_content,
    format_catalog_context,
    fuse_catalog_candidates,
    lexical_catalog_query,
)


class CatalogRagTests(unittest.TestCase):
    def test_embedding_content_contains_identity_not_price_or_stock(self):
        content = build_catalog_content(
            {
                "product_name": "Isabel I Chocolate",
                "variant_values": "8/8/10/12 mm",
                "sku": "ISABEL-CHOCO",
                "barcode": "12345",
                "handle": "isabel-i",
                "stock": "99",
                "price": "30000",
            }
        )
        self.assertIn("Isabel I Chocolate", content)
        self.assertIn("ISABEL-CHOCO", content)
        self.assertIn("12345", content)
        self.assertNotIn("99", content)
        self.assertNotIn("30000", content)

    def test_lexical_query_is_published_only_and_parameterized(self):
        search = lexical_catalog_query("Quiero Isabel chocolate", limit=3)
        self.assertIsNotNone(search)
        sql, params = search
        self.assertIn("published = true", sql)
        self.assertNotIn("isabel", sql.lower())
        self.assertEqual(params[-1], 3)
        self.assertIn("%isabel%", params)
        self.assertIn("%chocolate%", params)

    def test_lexical_identity_outranks_semantic_candidate(self):
        candidates = fuse_catalog_candidates(
            [{"variant_id": 1, "product_name": "Isabel I Chocolate", "similarity": 0}],
            [{"variant_id": 2, "product_name": "Pestañas sorpresa", "similarity": 0.91}],
            limit=3,
        )
        self.assertEqual(candidates[0].variant_id, 1)
        self.assertEqual(candidates[0].source, "lexical")

    def test_low_similarity_candidates_are_not_passed_to_the_model(self):
        candidates = fuse_catalog_candidates(
            [],
            [{"variant_id": 3, "product_name": "Resultado dudoso", "similarity": 0.31}],
            limit=3,
        )
        self.assertEqual(candidates, [])

    def test_context_tells_model_to_verify_live_commercial_facts(self):
        candidates = fuse_catalog_candidates(
            [{"variant_id": 1, "product_name": "Isabel I", "similarity": 0}],
            [],
            limit=3,
        )
        context = format_catalog_context(candidates)
        self.assertIn("no confirma stock ni precio", context)
        self.assertIn("herramienta de Tiendanube", context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
