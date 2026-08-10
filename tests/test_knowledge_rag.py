"""Offline tests for the reviewed Knowledge RAG boundary."""

import sys
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from knowledge_rag import (  # noqa: E402
    KNOWLEDGE_CHUNK_CHARS,
    approved_knowledge_rows,
    chunk_markdown,
    format_knowledge_context,
)


class KnowledgeRagTests(unittest.TestCase):
    def test_chunking_preserves_source_and_heading(self):
        chunks = chunk_markdown("politicas", "# Cambios\n" + "texto " * 250)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.source_id == "politicas" for chunk in chunks))
        self.assertTrue(all(chunk.section == "Cambios" for chunk in chunks))
        self.assertTrue(all(len(chunk.content) <= KNOWLEDGE_CHUNK_CHARS for chunk in chunks))

    def test_retrieval_excludes_drafts_retired_and_weak_matches(self):
        rows = [
            {"source_id": "ok", "status": "approved", "active": True, "similarity": 0.82},
            {"source_id": "draft", "status": "draft", "active": True, "similarity": 0.99},
            {"source_id": "old", "status": "approved", "active": False, "similarity": 0.99},
            {"source_id": "weak", "status": "approved", "active": True, "similarity": 0.20},
        ]
        accepted = approved_knowledge_rows(rows, limit=3)
        self.assertEqual([row["source_id"] for row in accepted], ["ok"])

    def test_formatted_context_keeps_freshness_boundary(self):
        context = format_knowledge_context([
            {
                "source_id": "politicas", "section": "Encargos",
                "content": "Se confirma un presupuesto antes de procesar.",
            }
        ])
        self.assertIn("politicas / Encargos", context)
        self.assertIn("no reemplaza datos vigentes", context)
        self.assertIn("Tiendanube o Isa", context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
