"""What does this turn actually need: Knowledge, the catalog, or the store?

Fred currently pays for retrieval AND live Tiendanube verification on every
agent turn, including turns the approved Knowledge base answers on its own.
This policy names that, so the spending can be measured before it is changed.

The policy decides nothing on its own yet -- these tests pin the
classification and the measurement, not a cut. Two properties are load-bearing:

  * the default is to spend. Anything unrecognised classifies as "catalog",
    so acting on this later can only remove work from turns positively
    identified as not needing it -- never on a guess.
  * a live need always outranks an approved answer. No document can report
    today's price or stock, so Knowledge never suppresses a live check.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402
from routing_policy import (  # noqa: E402
    DATA_CATALOG,
    DATA_KNOWLEDGE_ONLY,
    DATA_LIVE,
    build_product_lexicon,
    classify_turn_data_requirement,
)

# The real catalog snapshot, so the blocker tests below exercise the same
# lexicon production uses rather than a hand-picked stand-in.
LEXICON = app.product_lexicon()


class _Requirement:
    """Stand-in for a Knowledge dynamic requirement; only truthiness matters."""

    fact = "order_status"


class KnowledgeOnlyTurnsTests(unittest.TestCase):
    """Approved policy questions. Nothing here needs the store."""

    ANSWERED_BY_KNOWLEDGE = (
        "¿Hacen envíos?",
        "¿Cuál es el horario?",
        "¿Cómo funcionan las devoluciones?",
        "¿Dónde están ubicados?",
        "¿Puedo retirar por el showroom?",
        "¿Qué formas de pago aceptan?",
    )

    def test_a_governing_topic_makes_these_knowledge_only(self):
        for message in self.ANSWERED_BY_KNOWLEDGE:
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message,
                    governing_topic="commercial_operations",
                    knowledge_context="- [politicas / envios] Texto aprobado.",
                )
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)
                self.assertEqual(verdict["reason"], "governing_topic_answers_turn")

    def test_without_an_approved_answer_the_same_questions_stay_conservative(self):
        # This is the honest half: "¿Hacen envíos?" retrieves no governing
        # topic today (there is shipping content, but no chunk covering the
        # generic question). The policy must NOT invent one -- it falls back
        # to spending, and the logs then show a KB gap paying for lookups.
        # That is a content fix for Isa, not a code fix.
        for message in self.ANSWERED_BY_KNOWLEDGE:
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(message)
                self.assertEqual(verdict["data_required"], DATA_CATALOG)
                self.assertEqual(verdict["reason"], "no_governing_topic")

    def test_a_topic_without_retrieved_text_is_not_an_answer(self):
        verdict = classify_turn_data_requirement(
            "¿Cuál es el horario?", governing_topic="pickups_showroom",
            knowledge_context="   ",
        )
        self.assertEqual(verdict["data_required"], DATA_CATALOG)


class LiveDataTurnsTests(unittest.TestCase):
    """Commercial facts only the live store can answer truthfully."""

    def test_price_and_stock_questions_require_live_data(self):
        for message, reason in (
            ("¿Cuánto sale Isabel I?", "price_requested"),
            ("¿A qué precio está?", "price_requested"),
            ("¿Cuánto stock hay?", "stock_requested"),
            ("¿Qué colores quedan?", "stock_requested"),
            ("¿Tenés Isabel I?", "stock_requested"),
            ("¿Está disponible?", "stock_requested"),
        ):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(message)
                self.assertEqual(verdict["data_required"], DATA_LIVE)
                self.assertEqual(verdict["reason"], reason)

    def test_an_approved_topic_never_suppresses_a_live_commercial_question(self):
        # The dangerous inversion: a governing topic must not let Fred answer
        # a price question from a document.
        verdict = classify_turn_data_requirement(
            "¿Cuánto sale el envío de Isabel I?",
            governing_topic="commercial_operations",
            knowledge_context="- [politicas / envios] Texto aprobado.",
        )
        self.assertEqual(verdict["data_required"], DATA_LIVE)

    def test_a_knowledge_declared_dynamic_requirement_outranks_everything(self):
        # Knowledge itself said this answer is invalid without fresh data.
        verdict = classify_turn_data_requirement(
            "¿Dónde están ubicados?",
            governing_topic="pickups_showroom",
            knowledge_context="- [politicas / showroom] Texto aprobado.",
            dynamic_requirements=[_Requirement()],
        )
        self.assertEqual(verdict["data_required"], DATA_LIVE)
        self.assertEqual(verdict["reason"], "knowledge_requires_live_check")


class CatalogTurnsTests(unittest.TestCase):
    def test_wanting_a_product_without_naming_one_requires_the_catalog(self):
        for message in (
            "Busco pestañas naturales",
            "¿Qué modelo me recomendás?",
            "¿Me pasás el catálogo?",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    classify_turn_data_requirement(message)["data_required"],
                    DATA_CATALOG,
                )

    def test_deciding_to_buy_requires_live_data_not_merely_the_catalog(self):
        # A purchase can never be pinned to a SKU without live stock, so
        # purchase intent forces live. This is stricter than the earlier rule,
        # which stopped at "catalog" -- deliberately changed after the traffic
        # replay showed purchase turns slipping through as answerable.
        for message in ("Quiero comprar Isabel I", "Me interesa el color chocolate",
                        "Te voy a encargar 2", "Me llevo dos"):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(message)
                self.assertEqual(verdict["data_required"], DATA_LIVE)

    def test_anything_unrecognised_defaults_to_spending_not_to_skipping(self):
        for message in ("", "asdfgh", "🙂", "contame algo"):
            with self.subTest(message=message):
                self.assertEqual(
                    classify_turn_data_requirement(message)["data_required"],
                    DATA_CATALOG,
                )

    def test_the_classifier_is_pure(self):
        # Same inputs, same answer, no state carried between calls.
        first = classify_turn_data_requirement("¿Cuánto sale?")
        second = classify_turn_data_requirement("¿Cuánto sale?")
        self.assertEqual(first, second)


class LiveCallCounterTests(unittest.TestCase):
    def test_counting_is_optional_and_never_changes_the_result(self):
        # The counter argument is additive: without it, nothing happens.
        app._count_live_call(None)
        counter = {"count": 0}
        app._count_live_call(counter)
        app._count_live_call(counter)
        self.assertEqual(counter["count"], 2)

    def test_a_hostile_counter_never_raises(self):
        class Hostile(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("no")

        app._count_live_call(Hostile(count=1))  # must not raise

    def test_a_turn_that_never_touches_tiendanube_reports_zero(self):
        counter = {"count": 0}
        context = app._live_candidate_context(
            "sin product_id acá", "consulta de horario", live_calls=counter,
        )
        self.assertEqual(context, "")
        self.assertEqual(counter["count"], 0)


class RoutingLineTests(unittest.TestCase):
    def _emit(self, requirement=None, live_calls=None):
        import re

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_routing(requirement, live_calls=live_calls)
        body = stream.getvalue().strip()
        self.assertTrue(body.startswith("[FredRouting]"), body)
        return dict(re.findall(r"(\w+)=(.*?)(?=\s+\w+=|$)", body[len("[FredRouting]"):].strip()))

    def test_every_field_is_always_present(self):
        line = self._emit()
        self.assertEqual(set(line), {"intent", "data_required", "skipped_live", "reason"})

    def test_zero_live_calls_reports_skipped_true(self):
        line = self._emit(
            {"data_required": DATA_KNOWLEDGE_ONLY, "reason": "governing_topic_answers_turn"},
            live_calls={"count": 0},
        )
        self.assertEqual(line["data_required"], "knowledge_only")
        self.assertEqual(line["skipped_live"], "true")
        self.assertEqual(line["reason"], "governing_topic_answers_turn")

    def test_any_live_call_reports_skipped_false(self):
        line = self._emit(
            {"data_required": DATA_KNOWLEDGE_ONLY, "reason": "governing_topic_answers_turn"},
            live_calls={"count": 1},
        )
        # The measurement that matters: needed nothing live, spent anyway.
        self.assertEqual(line["data_required"], "knowledge_only")
        self.assertEqual(line["skipped_live"], "false")

    def test_missing_inputs_report_unknown_rather_than_crashing(self):
        line = self._emit(None, None)
        self.assertEqual(line["data_required"], "unknown")
        self.assertEqual(line["reason"], "unknown")


if __name__ == "__main__":
    unittest.main()


class BlockerCoverageTests(unittest.TestCase):
    """Every category that must FORCE dynamic data, one test each.

    These are the categories the traffic replay found leaking into
    knowledge_only. Each is a case where answering from an approved document
    would be answering the wrong question.
    """

    def _verdict(self, message):
        return classify_turn_data_requirement(message, product_lexicon=LEXICON)

    def test_a_named_catalog_product_blocks_a_knowledge_answer(self):
        for message in ("Foxy Cat eye?", "Las taylor son marrones?",
                        "Me gustarían los modelos twiggy", "Pestañas cluster mayormente"):
            with self.subTest(message=message):
                verdict = self._verdict(message)
                self.assertNotEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_an_existing_order_is_never_answered_from_a_document(self):
        for message in ("Sigue estando en pie el pedido?",
                        "Hola! Me llego el pedido",
                        "No tengo seguimiento de mi envío",
                        "Hice un pedido hace dos meses",
                        "El pedido sigue en una sucursal"):
            with self.subTest(message=message):
                verdict = self._verdict(message)
                self.assertEqual(verdict["data_required"], DATA_LIVE)
                self.assertEqual(verdict["intent"], "existing_order")

    def test_an_individual_claim_is_separated_from_the_returns_policy(self):
        # The distinction that matters: the policy question is Knowledge, the
        # case about this customer's own parcel is not.
        policy = classify_turn_data_requirement(
            "¿Cómo funcionan las devoluciones?",
            governing_topic="commercial_operations",
            knowledge_context="- [politicas / devoluciones] Texto aprobado.",
            product_lexicon=LEXICON,
        )
        self.assertEqual(policy["data_required"], DATA_KNOWLEDGE_ONLY)

        for message in ("Me mandaron el tono equivocado",
                        "Quiero devolver mi pedido",
                        "Me llegó roto"):
            with self.subTest(message=message):
                verdict = self._verdict(message)
                self.assertEqual(verdict["data_required"], DATA_LIVE)

    def test_price_wording_beyond_the_word_precio(self):
        for message in ("Me podrías enviar la cotización?",
                        "En cuanto me quedarían las 12 cajas?",
                        "Me pasás un presupuesto?",
                        "Hay algún descuento?"):
            with self.subTest(message=message):
                self.assertEqual(self._verdict(message)["data_required"], DATA_LIVE)

    def test_stock_wording_beyond_the_word_stock(self):
        for message in ("Cuales otros les entrarán?", "Reponen las Isabel?",
                        "Vuelven a entrar?"):
            with self.subTest(message=message):
                self.assertEqual(self._verdict(message)["data_required"], DATA_LIVE)

    def test_variant_and_size_choices_need_the_real_catalog(self):
        for message in ("una docena de cada tono", "Vienen en 10mm?",
                        "Que tamaños hay disponibles"):
            with self.subTest(message=message):
                self.assertNotEqual(
                    self._verdict(message)["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_anaphoric_references_are_never_knowledge_only(self):
        # "Las quiero" answered with silver hair flowers is the production bug
        # this category exists to prevent: the message carries no identity of
        # its own, so no document can be the right source.
        for message in ("Las dos cosas", "De esos productos por favor",
                        "Ese producto me sirve?", "Prefiero el anterior",
                        "Me quedo con la otra"):
            with self.subTest(message=message):
                self.assertNotEqual(
                    self._verdict(message)["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_every_blocker_outranks_a_confident_governing_topic(self):
        # The load-bearing property: Knowledge matching a topic must never be
        # able to override a signal that this turn needs real data.
        for message in ("Foxy Cat eye?", "Sigue en pie el pedido?",
                        "Me pasás la cotización?", "Hay stock?",
                        "Quiero comprar dos", "De esos productos por favor",
                        "una docena de cada tono"):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message,
                    governing_topic="commercial_operations",
                    knowledge_context="- [politicas / x] Texto aprobado y extenso.",
                    product_lexicon=LEXICON,
                )
                self.assertNotEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)

    def test_the_approved_policy_questions_still_pass_through(self):
        # The other half: hardening must not swallow the turns the bypass is
        # actually for.
        for message in ("¿Cuál es el horario?", "¿Dónde queda el showroom?",
                        "¿Cómo funcionan las devoluciones?",
                        "¿Qué formas de pago aceptan?",
                        "¿Hacen envíos a todo el país?"):
            with self.subTest(message=message):
                verdict = classify_turn_data_requirement(
                    message,
                    governing_topic="commercial_operations",
                    knowledge_context="- [politicas / x] Texto aprobado.",
                    product_lexicon=LEXICON,
                )
                self.assertEqual(verdict["data_required"], DATA_KNOWLEDGE_ONLY)
                self.assertEqual(verdict["intent"], "policy_question")

    def test_every_verdict_carries_all_three_fields(self):
        for message in ("Foxy Cat eye?", "hola", "¿Cuánto sale?", ""):
            with self.subTest(message=message):
                verdict = self._verdict(message)
                self.assertEqual(set(verdict), {"intent", "data_required", "reason"})
                self.assertTrue(all(verdict.values()))


class ProductLexiconTests(unittest.TestCase):
    def test_it_keeps_identifying_words_and_drops_generic_ones(self):
        lexicon = build_product_lexicon([
            "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
            "SHOOW TOOLS - FOXY #1",
            "Rare Beauty - Soft Pinch Liquid Blush",
        ])
        self.assertIn("isabel", lexicon)
        self.assertIn("foxy", lexicon)
        for generic in ("shoow", "tools", "beauty", "pack", "color"):
            self.assertNotIn(generic, lexicon)

    def test_a_frequent_family_name_is_kept_not_pruned_as_generic(self):
        # "foxy" spans dozens of products. A naive frequency cutoff would drop
        # it, and dropping it is exactly how "Foxy Cat eye?" leaked through.
        lexicon = build_product_lexicon(
            ["SHOOW TOOLS - FOXY #{}".format(n) for n in range(40)]
        )
        self.assertIn("foxy", lexicon)

    def test_an_empty_or_broken_catalog_degrades_to_no_detection(self):
        self.assertEqual(build_product_lexicon([]), frozenset())
        self.assertEqual(build_product_lexicon(None), frozenset())

    def test_detection_is_whole_word_not_substring(self):
        lexicon = build_product_lexicon(["SHOOW TOOLS - ISABEL I"])
        # "isabelita" is not "isabel"; a substring hit is a coincidence.
        self.assertEqual(
            classify_turn_data_requirement("hola isabelita", product_lexicon=lexicon)["intent"],
            "unknown",
        )
        self.assertEqual(
            classify_turn_data_requirement("tema isabel", product_lexicon=lexicon)["intent"],
            "product_named",
        )

    def test_the_real_snapshot_loads_and_finds_real_names(self):
        lexicon = app.product_lexicon()
        self.assertGreater(len(lexicon), 100)
        for name in ("isabel", "foxy", "taylor"):
            self.assertIn(name, lexicon)


class KnowledgeHealthLineTests(unittest.TestCase):
    def _emit(self, **kwargs):
        import re

        stream = io.StringIO()
        with redirect_stdout(stream):
            app._log_turn_knowledge(**kwargs)
        body = stream.getvalue().strip()
        self.assertTrue(body.startswith("[FredKnowledge]"), body)
        return dict(re.findall(
            r"(\w+)=(.*?)(?=\s+\w+=|$)", body[len("[FredKnowledge]"):].strip()))

    def test_every_field_is_always_present(self):
        line = self._emit(embedding_status="ok")
        self.assertEqual(
            set(line), {"embedding_status", "retrieval_hits", "embedding_error"})

    def test_a_healthy_turn_reports_ok_and_its_hits(self):
        line = self._emit(embedding_status="ok", retrieval_hits=6)
        self.assertEqual(line["embedding_status"], "ok")
        self.assertEqual(line["retrieval_hits"], "6")
        self.assertEqual(line["embedding_error"], "none")

    def test_a_failed_embedding_is_distinguishable_from_an_empty_kb(self):
        # Both end with Fred answering without Knowledge. Only this line says
        # which one happened -- an infrastructure outage or a content gap.
        outage = self._emit(
            embedding_status="failed", retrieval_hits=0, embedding_error="HTTPError")
        gap = self._emit(embedding_status="ok", retrieval_hits=0)
        self.assertEqual(outage["embedding_status"], "failed")
        self.assertEqual(outage["embedding_error"], "HTTPError")
        self.assertEqual(gap["embedding_status"], "ok")
        self.assertEqual(gap["embedding_error"], "none")

    def test_knowledge_disabled_reports_skipped(self):
        self.assertEqual(self._emit(embedding_status="skipped")["embedding_status"], "skipped")
