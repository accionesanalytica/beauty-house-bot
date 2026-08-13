"""Offline tests for the reviewed Knowledge RAG boundary."""

import sys
import tempfile
import unittest
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

from knowledge_rag import (  # noqa: E402
    DEFAULT_KNOWLEDGE_TOP_K,
    KNOWLEDGE_CHUNK_CHARS,
    KnowledgeChunk,
    KnowledgeRetrieval,
    approved_knowledge_rows,
    build_knowledge_retrieval,
    canonical_knowledge_embedding_text,
    chunk_markdown,
    collect_topic_obligations,
    enforce_knowledge_obligations,
    extract_order_number,
    format_knowledge_context,
    infer_dynamic_requirements,
    load_knowledge_chunks,
    retrieve_local_knowledge,
    recent_conversation_retrieval_query,
    retrieve_with_recent_context,
    validate_metadata,
)


class KnowledgeRagTests(unittest.TestCase):
    def test_real_knowledge_top_k_is_shared_and_bounded(self):
        self.assertEqual(DEFAULT_KNOWLEDGE_TOP_K, 6)

    def test_canonical_embedding_text_contains_only_semantic_fields(self):
        chunk = KnowledgeChunk(
            source_id="internal-source-id",
            section="Reembolsos",
            content="La devolución se revisa según el pedido.",
            metadata={
                "id": "internal-document-id",
                "topic": "commercial_operations",
                "keywords": ["devolución", "reembolso"],
                "approved_by": "Isa",
                "reviewed_at": "2026-08-11",
                "required_links": [{
                    "url": "https://example.com/obligatorio",
                    "link_type": "approved_static_link",
                }],
                "active": True,
            },
        )

        text = canonical_knowledge_embedding_text(chunk)

        self.assertIn("Topic: commercial operations", text)
        self.assertIn("Sección: Reembolsos", text)
        self.assertIn("Palabras clave: devolución, reembolso", text)
        self.assertIn("Contenido: La devolución", text)
        self.assertNotIn("internal-source-id", text)
        self.assertNotIn("internal-document-id", text)
        self.assertNotIn("Isa", text)
        self.assertNotIn("2026-08-11", text)
        self.assertNotIn("example.com", text)
        self.assertNotIn("active", text)

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

    def test_recursive_loader_validates_and_skips_non_indexed_documents(self):
        metadata = {
            "id": "test", "topic": "test", "knowledge_type": "fact",
            "source": "Isa", "approved_by": "Isa", "reviewed_at": "2026-08-11",
            "risk_level": "low", "requires_isa_confirmation": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            nested = Path(directory) / "facts"
            nested.mkdir()
            (nested / "ok.md").write_text(
                "---\n{}\n---\n# Dato\nContenido".format(__import__("json").dumps(metadata)),
                encoding="utf-8",
            )
            (Path(directory) / "playbook.md").write_text(
                '---\n{"index": false}\n---\n# No indexar', encoding="utf-8"
            )
            chunks = load_knowledge_chunks(directory)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].metadata["topic"], "test")

    def test_invalid_metadata_fails_closed(self):
        with self.assertRaises(ValueError):
            validate_metadata({"id": "incompleto"}, source="incompleto.md")

    def test_kit_obligations_are_grouped_and_deterministically_enforced(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        retrieval = retrieve_local_knowledge(
            "¿Qué trae el kit de retoque y cómo se carga el spray?",
            load_knowledge_chunks(root),
        )
        answer = enforce_knowledge_obligations(
            "La presentación incluye cinco sets.", retrieval.obligations
        )
        self.assertIn("no incluye", answer)
        self.assertIn("cosmético", answer)
        self.assertIn("orificio inferior", answer)
        self.assertIn("https://www.instagram.com/reel/DOdyhBHje7w/", answer)

    def test_lifting_retrieval_requires_approved_taylor_reference(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        retrieval = retrieve_local_knowledge(
            "Tengo lifting, ¿qué pestañas me recomendás?",
            load_knowledge_chunks(root),
        )
        self.assertIn("lashes_guidance", retrieval.obligations.topics)
        urls = {item["url"] for item in retrieval.obligations.required_links}
        self.assertIn("https://www.instagram.com/p/DZ3U5VGtnrX/", urls)

    def test_pickup_and_wholesale_retrieval_include_approved_links(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        chunks = load_knowledge_chunks(root)
        pickup = retrieve_local_knowledge("¿Cómo agendo un retiro?", chunks)
        wholesale = retrieve_local_knowledge("Quiero comprar por mayor", chunks)
        pickup_urls = {item["url"] for item in pickup.obligations.required_links}
        wholesale_urls = {item["url"] for item in wholesale.obligations.required_links}
        self.assertIn("https://calendar.app.google/Y5kYYhQtuQn8JTYU8", pickup_urls)
        self.assertIn("https://beautyhousemakeup.com/mayorista/", wholesale_urls)

    def test_unapproved_urls_are_removed_but_verified_dynamic_links_survive(self):
        obligations = collect_topic_obligations([
            {
                "metadata": {
                    "topic": "test",
                    "required_links": [{
                        "id": "approved", "link_type": "approved_static_link",
                        "url": "https://example.com/aprobado",
                    }],
                }
            }
        ])
        response = enforce_knowledge_obligations(
            "Mirá https://inventado.invalid y https://tienda.example/producto",
            obligations,
            verified_dynamic_links=["https://tienda.example/producto"],
        )
        self.assertNotIn("inventado.invalid", response)
        self.assertIn("https://tienda.example/producto", response)
        self.assertIn("https://example.com/aprobado", response)

    def test_all_knowledge_v1_documents_are_metadata_valid(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        chunks = load_knowledge_chunks(root)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(chunk.metadata.get("approved_by") == "Isa" for chunk in chunks))

    def test_dynamic_commercial_values_are_not_frozen_in_indexed_content(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        indexed = " ".join(chunk.content for chunk in load_knowledge_chunks(root)).lower()
        self.assertNotIn("usd 60", indexed)
        self.assertNotIn("usd 70", indexed)
        self.assertNotIn("cbu:", indexed)
        self.assertNotIn("alias:", indexed)
        self.assertNotIn("/checkout/", indexed)

    def test_primary_topic_matches_all_knowledge_benchmark_cases(self):
        import json

        root = Path(__file__).resolve().parents[1]
        chunks = load_knowledge_chunks(root / "knowledge")
        cases = [
            json.loads(line)
            for line in (root / "evals" / "knowledge_v1_cases.jsonl").read_text().splitlines()
            if line.strip()
        ]
        for case in cases:
            if case.get("scope") != "knowledge":
                continue
            retrieval = retrieve_local_knowledge(" ".join(case["messages"]), chunks, limit=6)
            with self.subTest(case=case["id"]):
                self.assertEqual(retrieval.governing_topic, case["expected_topics"][0])

    def test_secondary_topics_do_not_contribute_obligations(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        retrieval = retrieve_local_knowledge(
            "No encuentro mi pedido, lo coordiné por Instagram y no tengo número",
            load_knowledge_chunks(root), limit=6,
        )
        self.assertEqual(retrieval.governing_topic, "order_tracking")
        self.assertIn("pickups_showroom", retrieval.retrieved_topics)
        self.assertNotIn(
            "pickup-calendar",
            {item.get("id") for item in retrieval.obligations.required_links},
        )

    def test_recent_context_query_uses_only_customer_turns(self):
        query = recent_conversation_retrieval_query(
            "Lo coordiné por Instagram y no tengo número",
            [
                {"role": "user", "content": "No encuentro mi pedido"},
                {"role": "assistant", "content": "Inventé una política de retiro"},
            ],
        )
        self.assertIn("No encuentro mi pedido", query)
        self.assertIn("Lo coordiné por Instagram", query)
        self.assertNotIn("Inventé", query)

    def test_retrieval_falls_back_once_when_current_turn_has_no_topic(self):
        calls = []

        def retrieve(query):
            calls.append(query)
            if "pedido" in query:
                return KnowledgeRetrieval(governing_topic="order_tracking")
            return KnowledgeRetrieval()

        retrieval, query, used_fallback = retrieve_with_recent_context(
            "Lo coordiné por Instagram y no tengo número",
            [{"role": "user", "content": "No encuentro mi pedido"}],
            retrieve,
        )
        self.assertTrue(used_fallback)
        self.assertEqual(retrieval.governing_topic, "order_tracking")
        self.assertIn("No encuentro mi pedido", query)
        self.assertEqual(len(calls), 2)

    def test_retrieval_does_not_add_history_when_current_turn_is_confident(self):
        calls = []

        def retrieve(query):
            calls.append(query)
            return KnowledgeRetrieval(governing_topic="lashes_guidance")

        _, query, used_fallback = retrieve_with_recent_context(
            "¿Cómo limpio las pestañas?",
            [{"role": "user", "content": "No encuentro mi pedido"}],
            retrieve,
        )
        self.assertFalse(used_fallback)
        self.assertEqual(query, "¿Cómo limpio las pestañas?")
        self.assertEqual(calls, ["¿Cómo limpio las pestañas?"])

    def test_top_k_support_resolves_close_topic_without_losing_pickup_obligation(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        retrieval = retrieve_local_knowledge(
            "Voy a mandar una moto a buscar el pedido",
            load_knowledge_chunks(root), limit=6,
        )
        self.assertEqual(retrieval.governing_topic, "pickups_showroom")
        self.assertIn(
            "pickup-calendar",
            {item.get("id") for item in retrieval.obligations.required_links},
        )

    def test_generic_faq_cannot_govern_over_specific_topic(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        chunks = load_knowledge_chunks(root)
        self.assertEqual(
            retrieve_local_knowledge("¿Qué trae el kit de retoque?", chunks, limit=6).governing_topic,
            "touchup_kit",
        )
        self.assertEqual(
            retrieve_local_knowledge("¿Cómo limpio las pestañas?", chunks, limit=6).governing_topic,
            "lashes_guidance",
        )

    def test_no_confident_primary_topic_applies_no_obligations(self):
        metadata = {
            "topic": "topic_a", "knowledge_type": "fact",
            "required_disclosures": [{"id": "secret", "text": "No aplicar"}],
        }
        retrieval = build_knowledge_retrieval([
            {"source_id": "a", "content": "dato", "similarity": 0.05, "metadata": metadata}
        ], query="consulta sin relación")
        self.assertIsNone(retrieval.governing_topic)
        self.assertFalse(retrieval.obligations.required_disclosures)
        self.assertIn("No hay topic gobernante", retrieval.context)

    def test_dynamic_requirements_expose_verifier_and_safe_fallback(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        chunks = load_knowledge_chunks(root)
        order = retrieve_local_knowledge("¿Dónde está mi pedido?", chunks, limit=6)
        requirement = order.dynamic_requirements[0]
        self.assertEqual(requirement.fact, "order_status")
        self.assertEqual(requirement.verifier, "get_order_status")
        self.assertEqual(requirement.status, "missing_arguments")
        self.assertIn("order_number", requirement.missing_arguments)

        showroom = retrieve_local_knowledge("¿Puedo pasar hoy por el showroom?", chunks, limit=6)
        requirement = showroom.dynamic_requirements[0]
        self.assertEqual(requirement.fact, "calendar_availability")
        self.assertEqual(requirement.status, "unavailable_tool")

    def test_dynamic_requirement_keeps_ready_arguments_for_executor(self):
        retrieval = build_knowledge_retrieval([
            {
                "source_id": "order", "content": "seguimiento", "similarity": 0.9,
                "metadata": {
                    "topic": "order_tracking", "knowledge_type": "procedure",
                    "keywords": ["pedido", "seguimiento"],
                },
            }
        ], query="¿Dónde está el pedido 12345?")
        requirement = retrieval.dynamic_requirements[0]
        self.assertEqual(requirement.status, "ready")
        self.assertEqual(requirement.arguments, {"order_number": "12345"})

    def test_post_sale_case_reuses_existing_order_status_verifier(self):
        retrieval = build_knowledge_retrieval([
            {
                "source_id": "post-sale", "content": "devolución", "similarity": 0.9,
                "metadata": {
                    "topic": "commercial_operations", "knowledge_type": "procedure",
                    "keywords": ["devolución", "reembolso"],
                },
            }
        ], query="Quiero devolver el pedido 54321 y pedir reintegro")
        requirement = retrieval.dynamic_requirements[0]
        self.assertEqual(requirement.verifier, "get_order_status")
        self.assertEqual(requirement.status, "ready")
        self.assertEqual(requirement.arguments, {"order_number": "54321"})

    def test_natural_refund_wording_uses_same_order_status_verifier(self):
        requirement = infer_dynamic_requirements(
            "Quiero devolverlo y que me reintegren el dinero",
            "commercial_operations",
        )[0]
        self.assertEqual(requirement.fact, "order_status")
        self.assertEqual(requirement.verifier, "get_order_status")
        self.assertEqual(requirement.missing_arguments, ("order_number",))
        self.assertEqual(requirement.status, "missing_arguments")

    def test_order_tracking_topic_without_real_evidence_never_demands_order_number(self):
        # Root cause of a real production bug: "envío" is aliased to "pedido"
        # for topic keyword matching (see _tokens), which can tip a plain
        # shipping-cost/product question into the order_tracking topic even
        # though the customer never said anything tracking-shaped. Demanding
        # an order number is a hard customer-facing boundary and must require
        # real evidence in the customer's own message, not just a fuzzy
        # topic-classifier side effect.
        requirements = infer_dynamic_requirements(
            "cuanto fuera el envio si elijo esa opcion", "order_tracking",
        )
        self.assertEqual(requirements, ())

    def test_order_tracking_topic_with_real_evidence_still_asks_for_order_number(self):
        for query in (
            "¿cómo va mi pedido?",
            "quiero el tracking de mi orden",
            "¿dónde está mi compra?",
            "no me llegó todavía",
        ):
            requirement = infer_dynamic_requirements(query, "order_tracking")[0]
            self.assertEqual(requirement.fact, "order_status")
            self.assertEqual(requirement.missing_arguments, ("order_number",))

    def test_order_tracking_topic_with_an_actual_order_number_is_ready(self):
        requirement = infer_dynamic_requirements(
            "¿cómo va el pedido 12345?", "order_tracking",
        )[0]
        self.assertEqual(requirement.status, "ready")
        self.assertEqual(requirement.arguments, {"order_number": "12345"})

    def test_order_number_extraction_handles_non_adjacent_phrasing(self):
        # The original regex only matched digits immediately after
        # "orden"/"pedido"/"#" -- real customers say "el número es 1234" or
        # "mi pedido es el 1234", which previously fell through to the
        # model's own (unreliable) extraction instead of the deterministic,
        # zero-LLM-round path.
        for query in (
            "¿dónde está mi pedido? El número es 1234",
            "mi pedido es el 1234",
            "el numero de orden es 1234",
            "pedido: 1234",
        ):
            requirement = infer_dynamic_requirements(query, "order_tracking")[0]
            self.assertEqual(requirement.status, "ready", query)
            self.assertEqual(requirement.arguments, {"order_number": "1234"}, query)

    def test_extract_order_number_is_the_same_shared_helper(self):
        # app.py's deterministic tracking-flow router reuses this directly,
        # so it must behave identically to the dynamic-requirements path.
        self.assertEqual(extract_order_number("el número es 1234"), "1234")
        self.assertEqual(extract_order_number("no tengo ningún dato"), None)

    def test_order_number_extraction_does_not_swallow_an_unrelated_number(self):
        # "pedidos" (plural) must not match the singular-keyword boundary and
        # grab an unrelated number appearing later in the same sentence, even
        # when real tracking evidence ("mi pedido") is also present.
        requirement = infer_dynamic_requirements(
            "mi pedido no llegó. tengo 5 pedidos y necesito saber si llegan en 3000 minutos",
            "order_tracking",
        )[0]
        self.assertEqual(requirement.status, "missing_arguments")
        self.assertEqual(requirement.missing_arguments, ("order_number",))

    def test_escalation_triggers_are_conditional_not_topic_wide(self):
        root = Path(__file__).resolve().parents[1] / "knowledge"
        chunks = load_knowledge_chunks(root)
        payment = retrieve_local_knowledge("¿Cómo puedo pagar un encargo?", chunks, limit=6)
        special = retrieve_local_knowledge("Quiero encargar algo que no tienen", chunks, limit=6)
        self.assertFalse(payment.obligations.escalation_required)
        self.assertTrue(special.obligations.escalation_required)



if __name__ == "__main__":
    unittest.main(verbosity=2)
