"""Mock-only proof that Supabase retrieval keeps metadata connection alive."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))
os.environ.setdefault("GEMINI_API_KEY", "test-key")

import app  # noqa: E402


class FakeCursor:
    def __init__(self, connection, rows):
        self.connection = connection
        self.rows = rows
        self.closed = False

    def execute(self, query, arguments):
        if self.connection.closed:
            raise RuntimeError("connection already closed")
        self.query = query
        self.arguments = arguments

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)
        self.cursors = []
        self.closed = False

    def cursor(self):
        if self.closed:
            raise RuntimeError("connection already closed")
        cursor = FakeCursor(self, self.result_sets.pop(0))
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class KnowledgeSupabaseRouteTests(unittest.TestCase):
    def test_content_and_topic_obligations_are_read_before_connection_closes(self):
        metadata = {
            "id": "lashes-lifting",
            "topic": "lashes_guidance",
            "knowledge_type": "procedure",
            "source": "Isa",
            "approved_by": "Isa",
            "reviewed_at": "2026-08-11",
            "valid_until": "2027-08-11",
            "risk_level": "medium",
            "requires_isa_confirmation": False,
            "keywords": ["lifting"],
            "required_disclosures": [{
                "id": "no-band", "text": "Con lifting no se recomienda banda completa.",
                "when_any": ["lifting"],
            }],
            "required_links": [{
                "id": "taylor", "link_type": "approved_static_link",
                "url": "https://www.instagram.com/p/DZ3U5VGtnrX/",
                "when_any": ["lifting"],
            }],
        }
        connection = FakeConnection([
            [("lashes-lifting", "Lifting", "Usar Taylor cluster.", "approved", True, 0.95, metadata)],
            [(metadata,)],
        ])

        with patch.object(app, "KNOWLEDGE_RAG_ENABLED", True), patch.object(
            app, "KNOWLEDGE_RAG_SOURCE", "supabase"
        ), patch.object(
            app.psycopg2, "connect", return_value=connection
        ):
            retrieval = app.search_knowledge_bundle(
                "Tengo lifting", query_embedding=[0.1, 0.2]
            )

        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.cursors), 2)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))
        self.assertEqual(retrieval.governing_topic, "lashes_guidance")
        self.assertEqual(retrieval.rows[0]["metadata"]["knowledge_type"], "procedure")
        self.assertEqual(retrieval.rows[0]["metadata"]["approved_by"], "Isa")
        self.assertEqual(retrieval.rows[0]["metadata"]["valid_until"], "2027-08-11")
        self.assertIn("no-band", {
            item["id"] for item in retrieval.obligations.required_disclosures
        })
        self.assertIn("taylor", {
            item["id"] for item in retrieval.obligations.required_links
        })

    def test_supabase_failure_falls_back_to_reviewed_local_knowledge(self):
        with patch.object(app, "KNOWLEDGE_RAG_ENABLED", True), patch.object(
            app, "KNOWLEDGE_RAG_SOURCE", "supabase"
        ), patch.object(
            app, "_search_supabase_knowledge_bundle", return_value=None
        ), patch.object(
            app, "_search_local_knowledge_bundle",
            return_value=app.KnowledgeRetrieval(governing_topic="order_tracking"),
        ) as local:
            retrieval = app.search_knowledge_bundle("¿Dónde está mi pedido?")

        self.assertEqual(retrieval.governing_topic, "order_tracking")
        local.assert_called_once()

    def test_local_source_never_calls_supabase(self):
        with patch.object(app, "KNOWLEDGE_RAG_ENABLED", True), patch.object(
            app, "KNOWLEDGE_RAG_SOURCE", "local"
        ), patch.object(
            app, "_search_supabase_knowledge_bundle"
        ) as supabase, patch.object(
            app, "_search_local_knowledge_bundle",
            return_value=app.KnowledgeRetrieval(governing_topic="lashes_guidance"),
        ):
            retrieval = app.search_knowledge_bundle("¿Cómo limpio las pestañas?")

        self.assertEqual(retrieval.governing_topic, "lashes_guidance")
        supabase.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
