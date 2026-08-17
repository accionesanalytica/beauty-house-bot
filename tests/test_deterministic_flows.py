"""Tests for Fred Core's deterministic action layer: one persisted mode
(CHAT/MENU/CHECKOUT/TRACKING/ISA) is the only source of truth for routing.
switch(mode) in _fred_core_dispatch is the only place that decides which
action runs -- nothing here infers state by re-reading Fred's own prior
message text. Everything is offline: Tiendanube/WhatsApp/Supabase-backed
functions are mocked.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


def _state(**overrides):
    base = {
        "mode": "CHAT", "active_product_id": None, "active_product_name": None,
        "active_sku": None, "active_variant": None, "unit_price": None,
        "quantity": None, "delivery_method": None, "customer_name": None,
        "customer_email": None, "postal_code": None, "checkout_step": None,
        "order_number": None,
    }
    base.update(overrides)
    return base


class CatalogRetrievalQueryTests(unittest.TestCase):
    """CHAT's retrieval must keep the active_product context for a bare
    follow-up ("¿cómo quedan?") without it, the customer would have to repeat
    the product name on every message for Fred to know what they mean."""

    def test_active_product_is_prepended_when_known(self):
        query = app._catalog_retrieval_query("¿Cómo quedan?", [], "SHOOW TOOLS - ISABEL I")
        self.assertEqual(query, "SHOOW TOOLS - ISABEL I ¿Cómo quedan?")

    def test_no_active_product_leaves_the_message_untouched(self):
        query = app._catalog_retrieval_query("¿Cómo quedan?", [], "")
        self.assertEqual(query, "¿Cómo quedan?")

    def test_follow_up_marker_still_pulls_in_the_previous_customer_message(self):
        history = [{"role": "user", "content": "Busco pestañas naturales"}]
        query = app._catalog_retrieval_query("¿Y en chocolate?", history, "")
        self.assertEqual(query, "Busco pestañas naturales ¿Y en chocolate?")

    def test_active_product_and_follow_up_marker_combine(self):
        history = [{"role": "user", "content": "Busco pestañas naturales"}]
        query = app._catalog_retrieval_query("¿Y en chocolate?", history, "SHOOW TOOLS - ISABEL I")
        self.assertEqual(query, "SHOOW TOOLS - ISABEL I Busco pestañas naturales ¿Y en chocolate?")


class RenderFallbackMenuTests(unittest.TestCase):
    def test_generic_buy_label_without_an_active_product(self):
        text = app._render_fallback_menu()
        self.assertIn("1. Ver opciones de productos", text)
        self.assertIn("2. Comprar un producto", text)
        self.assertIn("3. Consultar un pedido", text)
        self.assertIn("4. Hablar con Isa", text)
        self.assertIn(app.FALLBACK_MENU_MARKER, text)

    def test_names_the_active_product_when_known(self):
        text = app._render_fallback_menu("SHOOW TOOLS - ISABEL I")
        self.assertIn("2. Comprar SHOOW TOOLS - ISABEL I", text)


class ExtractMenuSelectionTests(unittest.TestCase):
    def test_bare_digit_and_common_variants(self):
        for text, expected in (("1", "1"), ("2.", "2"), ("opcion 3", "3"), ("4", "4")):
            self.assertEqual(app._extract_menu_selection(text), expected)

    def test_free_text_is_not_a_selection(self):
        self.assertIsNone(app._extract_menu_selection("no sé, decime vos"))
        self.assertIsNone(app._extract_menu_selection("5"))


class FredCoreActiveProductFieldsTests(unittest.TestCase):
    def test_maps_candidate_shape_to_state_columns(self):
        fields = app._fred_core_active_product_fields({
            "sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I",
            "variant": "8mm", "unit_price": "30000",
        })
        self.assertEqual(fields, {
            "active_product_id": "ISABEL-1", "active_product_name": "SHOOW TOOLS - ISABEL I",
            "active_sku": "ISABEL-1", "active_variant": "8mm", "unit_price": "30000",
        })


class FredCoreTrackingTests(unittest.TestCase):
    @patch.object(app, "get_order_status")
    def test_handle_tracking_extracts_and_looks_up_the_order_number(self, get_status):
        get_status.return_value = {
            "found": True, "order_number": 1234, "status": "open",
            "shipping_status": "shipped", "payment_status": "paid",
            "fulfillment_status": "DISPATCHED", "shipping_type": "ship",
            "carrier": "Envío Nube", "tracking": "RR123456789AR",
        }
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_handle_tracking(7, "5491111111111", "el número es 1234", [])
        get_status.assert_called_once_with("1234")
        self.assertIn("RR123456789AR", reply)
        save_state.assert_called_once_with(7, mode="CHAT", order_number="1234")

    def test_handle_tracking_without_a_number_releases_instead_of_asking_again(self):
        # Deliberately changed: TRACKING used to answer anything non-numeric
        # with "decime sólo el número de orden", so a customer who changed the
        # subject could not get out -- every new question got the same reply.
        # Returning None hands this same message to the normal CHAT path, and
        # nobody has to send it twice.
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_handle_tracking(
                7, "5491111111111", "no tengo el número a mano", [])
        self.assertIsNone(reply)
        save_state.assert_called_once_with(7, mode="CHAT")

    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={"found": False, "message": "No encontré esa orden."})
    def test_order_not_found_escalates_and_returns_to_chat(self, get_status, queue_for_isa):
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_lookup_order(7, "5491111111111", "999999", [])
        queue_for_isa.assert_not_called()
        self.assertIn("no me aparece en el sistema", reply)
        self.assertIn(app.isa_contact_number(), reply)
        save_state.assert_called_once_with(7, mode="CHAT", order_number="999999")

    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 55, "status": "shipped",
        "shipping_status": "shipped", "tracking": None, "payment_status": "paid",
    })
    def test_shipped_without_tracking_escalates_as_a_contradiction(self, get_status, queue_for_isa):
        with patch.object(app, "save_fred_core_state"):
            reply = app._fred_core_lookup_order(7, "5491111111111", "55", [])
        queue_for_isa.assert_not_called()
        self.assertIn("inconsistencia", reply)

    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 77, "status": "open",
        "shipping_status": "shipped", "payment_status": "paid",
            "fulfillment_status": "DISPATCHED", "shipping_type": "ship", "tracking": "RR1AR",
    })
    def test_found_with_tracking_never_escalates(self, get_status):
        with patch.object(app, "_queue_for_isa") as queue_for_isa, patch.object(app, "save_fred_core_state"):
            reply = app._fred_core_lookup_order(7, "5491111111111", "77", [])
        queue_for_isa.assert_not_called()
        self.assertIn("RR1AR", reply)


class FredCoreSearchProductsTests(unittest.TestCase):
    RESULTS = [
        {"product_id": 1, "name": "SHOOW TOOLS - NATURAL SHOOW", "variants": [{"sku": "NATURAL-1", "quantity": 8}]},
        {"product_id": 2, "name": "SHOOW TOOLS - ISABEL I", "variants": [{"sku": "ISABEL-1", "quantity": 3}]},
    ]

    @patch.object(app, "search_available_products")
    def test_multiple_candidates_are_listed_without_adopting_one(self, search):
        search.return_value = self.RESULTS
        reply, resolved = app._fred_core_search_products("algo natural")
        self.assertIn("1. SHOOW TOOLS - NATURAL SHOOW", reply)
        self.assertIn("2. SHOOW TOOLS - ISABEL I", reply)
        self.assertIsNone(resolved)

    @patch.object(app, "search_available_products", return_value=[RESULTS[1]])
    def test_a_single_candidate_is_returned_for_adoption(self, search):
        reply, resolved = app._fred_core_search_products("isabel")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["product_name"], "SHOOW TOOLS - ISABEL I")

    @patch.object(app, "search_available_products", return_value=[])
    def test_no_candidates_offers_to_talk_more_or_isa(self, search):
        reply, resolved = app._fred_core_search_products("algo muy raro")
        self.assertIn("Isa", reply)
        self.assertIsNone(resolved)

    def test_empty_query_asks_what_the_customer_wants(self):
        reply, resolved = app._fred_core_search_products("")
        self.assertIsNone(resolved)
        self.assertIn("Contame", reply)


class StrongTrackingTriggerTests(unittest.TestCase):
    """An explicit request to check an order must enter TRACKING; a mere
    passing mention of "mi pedido" must not (e.g. "llegó, gracias")."""

    def _matches(self, text):
        return bool(app._STRONG_TRACKING_TRIGGER_RE.search(app._knowledge_normalise(text)))

    def test_explicit_check_requests_match(self):
        for text in (
            "Quiero consultar mi pedido",
            "Quiero saber el estado de mi pedido",
            "¿Podés decirme el estado de mi compra?",
            "Quiero saber sobre mi orden",
            "¿Cómo rastreo mi pedido?",
            "¿Tienen tracking de mi pedido?",
        ):
            self.assertTrue(self._matches(text), text)

    def test_passing_mentions_do_not_match(self):
        for text in ("Mi pedido llegó perfecto, gracias", "Me encantó mi pedido anterior"):
            self.assertFalse(self._matches(text), text)


class LiveProductCandidateTests(unittest.TestCase):
    """A real product can have more than one live variant (e.g. two lash
    lengths of the same model) -- that must not stop Fred Core from
    identifying the PRODUCT, even though the exact SKU stays unresolved."""

    MULTI_VARIANT_CONTEXT = (
        "Disponibilidad Tiendanube verificada para candidatas recuperadas: "
        "estas opciones tienen stock positivo ahora.\n"
        "- SHOOW TOOLS - ISABEL I | variantes disponibles: Black MIXED (8/10/12/14mm), "
        "Black 8mm/10mm | Link: https://example.com/isabel-i"
    )
    SINGLE_VARIANT_CONTEXT = (
        "Disponibilidad Tiendanube verificada para candidatas recuperadas.\n"
        "- SHOOW TOOLS - TAYLOR | variantes disponibles: 10mm | SKU: TAYLOR-1"
    )

    def test_multi_variant_product_is_identified_by_name_without_a_sku(self):
        candidate = app._live_product_candidate(self.MULTI_VARIANT_CONTEXT, "Tengo dudas sobre Isabel I")
        self.assertEqual(candidate, {
            "sku": "", "product_name": "SHOOW TOOLS - ISABEL I", "variant": "", "unit_price": None,
        })

    @patch.object(app, "get_stock", return_value={"found": True, "status": "in_stock", "sku": "TAYLOR-1"})
    def test_single_variant_product_still_resolves_a_real_sku(self, get_stock):
        candidate = app._live_product_candidate(self.SINGLE_VARIANT_CONTEXT, "Quiero Taylor")
        get_stock.assert_called_once_with("TAYLOR-1")
        self.assertEqual(candidate["sku"], "TAYLOR-1")

    def test_no_match_without_the_products_own_name_in_the_message(self):
        candidate = app._live_product_candidate(self.MULTI_VARIANT_CONTEXT, "Si me gustaria comprar 4")
        self.assertEqual(candidate, {})


class FredCoreEnterCheckoutTests(unittest.TestCase):
    @patch.object(app, "_start_sales_intake", return_value="¿Qué modelo o variante querés llevar?")
    def test_no_active_product_asks_which_one(self, start_intake):
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_enter_checkout(7, "5491111111111", _state())
        start_intake.assert_called_once_with(7, quantity=0)
        save_state.assert_called_once_with(7, mode="CHECKOUT", quantity=None)
        self.assertIn("modelo o variante", reply)

    @patch.object(app, "_start_sales_intake", return_value="__FULFILLMENT_BUTTONS__")
    def test_product_known_but_sku_ambiguous_keeps_name_and_quantity(self, start_intake):
        # The real bug this guards: a product with more than one live variant
        # (active_sku empty) must not be treated as "no product identified at
        # all" -- the quantity the customer already gave must not be dropped,
        # and Fred must not restart the checkout from a blank slate.
        state = _state(active_product_name="SHOOW TOOLS - ISABEL I")
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_enter_checkout(7, "5491111111111", state, quantity=4)
        save_state.assert_called_once_with(7, mode="CHECKOUT", quantity=4)
        start_intake.assert_called_once_with(
            7, {"product_name": "SHOOW TOOLS - ISABEL I", "sku": "", "variant": "", "unit_price": None},
            quantity=4,
        )
        self.assertEqual(reply, "__FULFILLMENT_BUTTONS__")

    @patch.object(app, "get_stock", return_value={"found": True, "status": "out_of_stock"})
    def test_out_of_stock_on_revalidation_never_starts_the_intake(self, get_stock):
        state = _state(active_sku="ISABEL-1", active_product_name="SHOOW TOOLS - ISABEL I")
        with patch.object(app, "_start_sales_intake") as start_intake:
            reply = app._fred_core_enter_checkout(7, "5491111111111", state)
        start_intake.assert_not_called()
        self.assertIn("ya no tiene stock", reply)

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "product_name": "SHOOW TOOLS - ISABEL I",
        "variant": "8mm", "price": "30000",
    })
    def test_in_stock_anchors_to_the_active_product_and_starts_the_intake(self, get_stock):
        state = _state(active_sku="ISABEL-1", active_product_name="SHOOW TOOLS - ISABEL I")
        with patch.object(app, "save_fred_core_state") as save_state, patch.object(
            app, "_start_sales_intake", return_value="__FULFILLMENT_BUTTONS__",
        ) as start_intake:
            app._fred_core_enter_checkout(7, "5491111111111", state, quantity=4)
        get_stock.assert_called_once_with("ISABEL-1")
        save_state.assert_called_once_with(
            7, mode="CHECKOUT", quantity=4,
            active_product_id="ISABEL-1", active_product_name="SHOOW TOOLS - ISABEL I",
            active_sku="ISABEL-1", active_variant="8mm", unit_price="30000",
        )
        start_intake.assert_called_once_with(
            7,
            {
                "product_name": "SHOOW TOOLS - ISABEL I", "sku": "ISABEL-1",
                "variant": "8mm", "unit_price": "30000",
            },
            quantity=4,
        )

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "product_name": "SHOOW TOOLS - ISABEL I",
        "variant": "8mm", "price": "30000",
    })
    def test_same_message_details_go_straight_to_the_summary(self, get_stock):
        state = _state(active_sku="ISABEL-1", active_product_name="SHOOW TOOLS - ISABEL I")
        with patch.object(app, "save_fred_core_state"), patch.object(
            app, "_start_sales_intake", return_value="pedir datos",
        ), patch.object(
            app, "_apply_sale_details_from_same_message", return_value="Resumen listo",
        ) as apply_details:
            reply = app._fred_core_enter_checkout(
                7, "5491111111111", state, quantity=2,
                message_text="Quiero 2, envío, Ana Pérez, ana@example.com",
            )
        apply_details.assert_called_once_with(7, "Quiero 2, envío, Ana Pérez, ana@example.com")
        self.assertEqual(reply, "Resumen listo")


class FredCoreHandleCheckoutTests(unittest.TestCase):
    @patch.object(app, "get_active_sales_intake", return_value=None)
    def test_no_active_intake_resets_and_falls_back_to_chat(self, get_intake):
        with patch.object(app, "reset_fred_core_checkout") as reset_checkout:
            result = app._fred_core_handle_checkout(7, "5491111111111", "hola", [])
        reset_checkout.assert_called_once_with(7)
        self.assertIsNone(result)

    @patch.object(app, "get_active_sales_intake")
    @patch.object(app, "_handle_sales_intake", return_value=False)
    def test_a_different_product_releases_checkout_and_clears_the_active_product(
        self, handle_intake, get_intake,
    ):
        get_intake.return_value = {"status": "quantity"}
        with patch.object(app, "reset_fred_core_checkout") as reset_checkout, patch.object(
            app, "save_fred_core_state",
        ) as save_state:
            result = app._fred_core_handle_checkout(7, "5491111111111", "mejor quiero Taylor", [])
        reset_checkout.assert_called_once_with(7)
        save_state.assert_called_once_with(
            7, active_product_id=None, active_product_name=None,
            active_sku=None, active_variant=None, unit_price=None,
        )
        self.assertIsNone(result)

    @patch.object(app, "get_active_sales_intake")
    @patch.object(app, "_handle_sales_intake", return_value=True)
    def test_ongoing_intake_mirrors_its_fields_into_fred_core(self, handle_intake, get_intake):
        get_intake.side_effect = [
            {"status": "quantity"},
            {"status": "confirmation", "quantity": 4, "fulfillment": "shipping",
             "customer_name": "Ana", "customer_email": "ana@example.com"},
        ]
        with patch.object(app, "save_fred_core_state") as save_state:
            result = app._fred_core_handle_checkout(7, "5491111111111", "4", [])
        self.assertEqual(result, "__HANDLED_NO_REPLY__")
        save_state.assert_called_once_with(
            7, quantity=4, delivery_method="shipping",
            customer_name="Ana", customer_email="ana@example.com",
            checkout_step="confirmation",
        )

    @patch.object(app, "get_active_sales_intake")
    @patch.object(app, "_handle_sales_intake", return_value=True)
    def test_intake_resolved_ready_for_isa_returns_to_chat(self, handle_intake, get_intake):
        get_intake.side_effect = [{"status": "confirmation"}, None]
        with patch.object(app, "reset_fred_core_checkout") as reset_checkout:
            result = app._fred_core_handle_checkout(7, "5491111111111", "confirmo", [])
        reset_checkout.assert_called_once_with(7)
        self.assertEqual(result, "__HANDLED_NO_REPLY__")

    @patch.object(app, "get_active_sales_intake", return_value={"status": "fulfillment"})
    @patch.object(app, "_handle_sales_intake", return_value=None)
    def test_an_interruption_question_is_released_to_chat_without_touching_anything(
        self, handle_intake, get_intake,
    ):
        # The real reported bug: "¿Envío o retiro?" -> "Antes, ¿son
        # reutilizables?" must not cancel the checkout or clear the active
        # product -- it must resume exactly where it was on the next message.
        with patch.object(app, "reset_fred_core_checkout") as reset_checkout, patch.object(
            app, "save_fred_core_state",
        ) as save_state:
            result = app._fred_core_handle_checkout(
                7, "5491111111111", "Antes, ¿son reutilizables?", [],
            )
        reset_checkout.assert_not_called()
        save_state.assert_not_called()
        self.assertIsNone(result)


class LashTypeFromCatalogTests(unittest.TestCase):
    """The approved Knowledge gives opposite guidance for cluster vs banda
    (reutilización, lifting), so the type is decided from the product's own
    catalog record and handed to the model as a fact -- never inferred."""

    def test_cluster_is_recognized_from_its_real_description(self):
        # Wording taken from the live Isabel I record.
        self.assertEqual(
            app._lash_type_from_catalog(
                "SHOOW TOOLS - ISABEL I",
                "Su diseño está compuesto por delicados grupos de fibras (3 grupos)...",
            ),
            "cluster (grupos de fibras)",
        )

    def test_banda_is_recognized_from_its_real_description(self):
        self.assertEqual(
            app._lash_type_from_catalog(
                "SHOOW TOOLS - SHOOW YOU (10PAIRS)",
                "Pestañas de banda intermedia con pelos cortos al inicio.",
            ),
            "banda completa",
        )

    def test_pairs_presentation_defaults_to_banda(self):
        self.assertEqual(
            app._lash_type_from_catalog("BH - PESTAÑAS 10 PARES", "Producto importado."),
            "banda completa",
        )

    def test_ambiguous_or_silent_records_assert_nothing(self):
        self.assertEqual(app._lash_type_from_catalog("ALGO", "Sin detalles de formato."), "")
        # Both signals present -> refuse to label rather than guess.
        self.assertEqual(
            app._lash_type_from_catalog("MIX", "Trae clusters y también banda completa."), "",
        )


class NaturalAffirmationTests(unittest.TestCase):
    """A person answering "sí"/"dale" to a question Fred just asked must be
    understood as answering THAT question. These are the cheap, obvious cases
    resolved without a model round; anything richer falls through to CHAT."""

    def test_common_affirmations(self):
        for text in (
            "si", "sí", "Sí", "sii", "dale", "ok", "okey", "listo", "perfecto",
            "confirmo", "avancemos", "hagamoslo", "de una", "dale si",
            "sí, dale", "genial", "obvio", "correcto", "va",
        ):
            self.assertTrue(app._reads_as_affirmation(text), text)

    def test_common_negations(self):
        for text in ("no", "no gracias", "mejor no", "cancelalo", "dejalo", "olvidalo"):
            self.assertTrue(app._reads_as_negation(text), text)

    def test_real_messages_are_neither(self):
        for text in (
            "Quiero comprar 4", "¿son reutilizables?", "Luis Vera, luis@example.com",
            "envío", "1340", "mejor 3",
        ):
            self.assertFalse(app._reads_as_affirmation(text), text)
            self.assertFalse(app._reads_as_negation(text), text)


class SalesMissingStepTests(unittest.TestCase):
    def test_everything_missing_is_asked_in_one_message(self):
        reply = app._sales_missing_step({})
        for expected in ("cuántas unidades", "envío o retiro", "nombre y apellido", "tu email"):
            self.assertIn(expected, reply)
        self.assertIn("todo junto", reply)

    def test_only_fulfillment_missing_asks_in_words_by_default(self):
        # Buttons are a shortcut, never a requirement -- Fred reads "envío"
        # written naturally, so the default flow stays conversational.
        intake = {"quantity": 4, "customer_name": "Ana", "customer_email": "a@b.com"}
        with patch.object(app, "FULFILLMENT_BUTTONS_ENABLED", False):
            self.assertIn("envío o retiro", app._sales_missing_step(intake))
        with patch.object(app, "FULFILLMENT_BUTTONS_ENABLED", True):
            self.assertEqual(app._sales_missing_step(intake), "__FULFILLMENT_BUTTONS__")

    def test_a_single_remaining_field_is_asked_alone(self):
        reply = app._sales_missing_step({
            "quantity": 4, "fulfillment": "shipping", "customer_name": "Ana",
        })
        self.assertIn("únicamente", reply)
        self.assertIn("tu email", reply)


class LooksLikeAnInterruptionQuestionTests(unittest.TestCase):
    def test_questions_are_interruptions(self):
        for text in ("¿Son reutilizables?", "Antes, ¿esto sirve para lifting?", "Como se aplican?"):
            self.assertTrue(app._looks_like_an_interruption_question(text), text)

    def test_plain_answers_are_not_interruptions(self):
        for text in ("Envío", "2", "Juan Pérez, juan@example.com", "confirmo"):
            self.assertFalse(app._looks_like_an_interruption_question(text), text)


class HandleSalesIntakeInterruptionTests(unittest.TestCase):
    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "record_bot_message")
    def test_unrelated_question_leaves_the_intake_untouched(self, record_message, send_message):
        intake = {
            "status": "fulfillment", "product_request": "SHOOW TOOLS - ISABEL I",
            "selected_sku": "ISABEL-1", "selected_variant": "", "unit_price": "30000",
            "quantity": 4, "fulfillment": None, "customer_name": None, "customer_email": None,
        }
        with patch.object(app, "set_sales_intake_quantity") as set_quantity, patch.object(
            app, "set_sales_intake_fulfillment",
        ) as set_fulfillment, patch.object(app, "cancel_sales_intake") as cancel_intake:
            result = app._handle_sales_intake(
                7, "5491111111111", "Antes, ¿son reutilizables?", intake, [],
            )
        self.assertIsNone(result)
        set_quantity.assert_not_called()
        set_fulfillment.assert_not_called()
        cancel_intake.assert_not_called()
        send_message.assert_not_called()
        record_message.assert_not_called()


class FredCoreIsaHandoffTests(unittest.TestCase):
    def test_handoff_hands_over_isa_contact_without_creating_a_case(self):
        # No consultation, no pending, no relay: the customer writes to Isa.
        state = _state(active_product_name="SHOOW TOOLS - ISABEL I", order_number="1234")
        with patch.object(app, "save_fred_core_state") as save_state, patch.object(
            app, "_queue_for_isa",
        ) as queue_for_isa:
            reply = app._fred_core_run_isa_handoff(7, "5491111111111", [], state)
        queue_for_isa.assert_not_called()
        save_state.assert_called_once_with(7, mode="CHAT")
        self.assertIn("Isa", reply)
        self.assertIn(app.isa_contact_number(), reply)


class FredCoreMenuDispatchTests(unittest.TestCase):
    @patch.object(app, "search_available_products", return_value=[])
    def test_selection_1_searches_using_recent_customer_message(self, search):
        with patch.object(app, "save_fred_core_state"):
            app._fred_core_handle_menu(
                7, "5491111111111", "1", _state(),
                [{"role": "user", "content": "pestañas"}, {"role": "assistant", "content": "..."}],
            )
        search.assert_called_once_with("pestañas")

    @patch.object(app, "_start_sales_intake")
    def test_selection_2_enters_checkout(self, start_intake):
        with patch.object(app, "save_fred_core_state"):
            app._fred_core_handle_menu(7, "5491111111111", "2", _state(), [])
        start_intake.assert_called_once_with(7, quantity=0)

    def test_selection_3_asks_for_order_number_and_sets_tracking_mode(self):
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_handle_menu(7, "5491111111111", "3", _state(), [])
        save_state.assert_called_once_with(7, mode="TRACKING")
        self.assertEqual(reply, app.ORDER_NUMBER_PROMPT_TEXT)

    def test_unrecognized_reply_releases_back_to_chat_instead_of_re_showing_the_menu(self):
        # MENU only ever consumes an explicit 1-4 selection -- anything else
        # is a real question, and holding it hostage in the menu was the
        # exact reported bug (a lifting question got swallowed by a re-shown
        # menu instead of ever reaching the model).
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_handle_menu(
                7, "5491111111111", "Me hice un lifting ayer, ¿estas me sirven?",
                _state(active_product_name="Isabel I"), [],
            )
        self.assertIsNone(reply)
        save_state.assert_called_once_with(7, mode="CHAT")


class FredCoreSwitchModeTests(unittest.TestCase):
    """switch(mode) itself: the only place a mode is dispatched."""

    def test_menu_mode_is_legacy_and_self_heals_to_chat(self):
        # MENU no longer exists in the runtime; an old conversation stuck
        # there is corrected and continues as a normal CHAT turn.
        with patch.object(app, "save_fred_core_state") as save_state:
            result = app._fred_core_dispatch("MENU", 7, "5491111111111", "1", _state(), [])
        save_state.assert_called_once_with(7, mode="CHAT")
        self.assertIsNone(result)

    def test_checkout_mode_dispatches_to_handle_checkout(self):
        with patch.object(app, "_fred_core_handle_checkout", return_value="__HANDLED_NO_REPLY__") as handle_checkout:
            result = app._fred_core_dispatch("CHECKOUT", 7, "5491111111111", "4", _state(), [])
        handle_checkout.assert_called_once()
        self.assertEqual(result, "__HANDLED_NO_REPLY__")

    def test_tracking_mode_dispatches_to_handle_tracking(self):
        with patch.object(app, "_fred_core_handle_tracking", return_value="tracking-reply") as handle_tracking:
            result = app._fred_core_dispatch("TRACKING", 7, "5491111111111", "1234", _state(), [])
        handle_tracking.assert_called_once()
        self.assertEqual(result, "tracking-reply")

    def test_isa_mode_is_transient_and_self_heals_to_chat(self):
        with patch.object(app, "save_fred_core_state") as save_state:
            result = app._fred_core_dispatch("ISA", 7, "5491111111111", "hola", _state(), [])
        save_state.assert_called_once_with(7, mode="CHAT")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
