"""Contract test for a real production risk found in the order-tracking
audit: build_turn_messages bounds rag_context to MAX_RAG_CONTEXT_CHARS by
truncating the *tail* of the string (see context_builder._bounded_text).
Verified live facts (a real get_order_status/get_stock result, injected as
dynamic_context) must never be the part that gets silently cut just because
static approved knowledge prose came first and pushed it past the limit.

This is a source-text contract rather than a full webhook integration test:
the ordering lives inline inside a large function in bot/app.py, and what
actually matters is which piece of text is concatenated first.
"""

import sys
import unittest
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"


class DynamicContextSurvivesTruncationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (BOT_DIR / "app.py").read_text(encoding="utf-8")

    def test_dynamic_context_is_joined_before_static_knowledge_prose(self):
        marker = "knowledge_context = \"\\n\\n\".join("
        start = self.source.index(marker)
        # The join's argument tuple/generator must name dynamic_context
        # before knowledge_bundle.context, not after.
        window = self.source[start:start + 300]
        dynamic_pos = window.index("dynamic_context")
        static_pos = window.index("knowledge_bundle.context")
        self.assertLess(
            dynamic_pos, static_pos,
            "dynamic_context (verified live facts) must be listed before "
            "knowledge_bundle.context (static prose) so truncation trims "
            "the prose first, not the verified data.",
        )


if __name__ == "__main__":
    unittest.main()
