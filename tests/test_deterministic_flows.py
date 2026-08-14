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
            "shipping_status": "shipped", "tracking": "RR123456789AR", "payment_status": "paid",
        }
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_handle_tracking(7, "5491111111111", "el número es 1234", [])
        get_status.assert_called_once_with("1234")
        self.assertIn("RR123456789AR", reply)
        save_state.assert_called_once_with(7, mode="CHAT", order_number="1234")

    def test_handle_tracking_without_a_number_asks_again_and_stays_in_tracking(self):
        reply = app._fred_core_handle_tracking(7, "5491111111111", "no tengo el número a mano", [])
        self.assertEqual(reply, "No pasa nada, decime sólo el número de orden y lo reviso. 😊")

    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={"found": False, "message": "No encontré esa orden."})
    def test_order_not_found_escalates_and_returns_to_chat(self, get_status, queue_for_isa):
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_lookup_order(7, "5491111111111", "999999", [{"role": "user", "content": "hola"}])
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "bot_fallback")
        self.assertEqual(queue_for_isa.call_args.kwargs["conversation_context"], [{"role": "user", "content": "hola"}])
        self.assertIn("no me aparece en el sistema", reply)
        save_state.assert_called_once_with(7, mode="CHAT", order_number="999999")

    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 55, "status": "shipped",
        "shipping_status": "shipped", "tracking": None, "payment_status": "paid",
    })
    def test_shipped_without_tracking_escalates_as_a_contradiction(self, get_status, queue_for_isa):
        with patch.object(app, "save_fred_core_state"):
            reply = app._fred_core_lookup_order(7, "5491111111111", "55", [])
        queue_for_isa.assert_called_once()
        self.assertIn("inconsistencia", reply)

    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 77, "status": "open",
        "shipping_status": "shipped", "tracking": "RR1AR", "payment_status": "paid",
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


class FredCoreIsaHandoffTests(unittest.TestCase):
    @patch.object(app, "_queue_for_isa")
    def test_snapshot_includes_active_product_and_order_number(self, queue_for_isa):
        state = _state(active_product_name="SHOOW TOOLS - ISABEL I", order_number="1234")
        with patch.object(app, "save_fred_core_state") as save_state:
            reply = app._fred_core_run_isa_handoff(7, "5491111111111", [{"role": "user", "content": "hola"}], state)
        summary = queue_for_isa.call_args.args[3]
        self.assertIn("SHOOW TOOLS - ISABEL I", summary)
        self.assertIn("1234", summary)
        save_state.assert_called_once_with(7, mode="CHAT")
        self.assertIn("Isa", reply)


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

    @patch.object(app, "_queue_for_isa")
    def test_selection_4_escalates_to_isa(self, queue_for_isa):
        with patch.object(app, "save_fred_core_state"):
            reply = app._fred_core_handle_menu(7, "5491111111111", "4", _state(), [])
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "human_handoff")
        self.assertIn("Isa", reply)

    def test_unrecognized_reply_re_shows_the_menu(self):
        reply = app._fred_core_handle_menu(7, "5491111111111", "no sé", _state(active_product_name="Isabel I"), [])
        self.assertEqual(reply, app._render_fallback_menu("Isabel I"))


class FredCoreSwitchModeTests(unittest.TestCase):
    """switch(mode) itself: the only place a mode is dispatched."""

    def test_menu_mode_dispatches_to_handle_menu(self):
        with patch.object(app, "_fred_core_handle_menu", return_value="menu-reply") as handle_menu:
            result = app._fred_core_dispatch("MENU", 7, "5491111111111", "1", _state(), [])
        handle_menu.assert_called_once()
        self.assertEqual(result, "menu-reply")

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
