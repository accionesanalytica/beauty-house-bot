"""Tests for the new deterministic action layer ("LLM responde, flows
ejecutan"): the universal fallback menu and the four flows it routes to
(ver productos, comprar, consultar pedido, hablar con Isa). Everything here
runs with zero real network/DB calls -- Tiendanube, WhatsApp and Supabase
functions are mocked -- and asserts the flow never needs an LLM round to
resolve a menu selection or a tracking request.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


MENU_TEXT = app._render_fallback_menu()
PRODUCTS_LIST_TEXT = (
    "Estas son las opciones que encontré:\n"
    "1. SHOOW TOOLS - NATURAL SHOOW — $36.000, disponible\n"
    "2. SHOOW TOOLS - ISABEL I — disponible\n"
    "3. Hablar con Isa"
)
ORDER_PROMPT_HISTORY = [{"role": "assistant", "content": app.ORDER_NUMBER_PROMPT_TEXT}]


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


class TrackingFlowRoutingTests(unittest.TestCase):
    @patch.object(app, "get_order_status")
    def test_continuing_an_open_order_number_prompt_looks_up_immediately(self, get_status):
        get_status.return_value = {
            "found": True, "order_number": 1234, "status": "open",
            "shipping_status": "shipped", "tracking": "RR123456789AR", "payment_status": "paid",
        }
        reply = app._try_deterministic_flow(7, "5491111111111", "1234", ORDER_PROMPT_HISTORY)

        get_status.assert_called_once_with("1234")
        self.assertIn("RR123456789AR", reply)

    def test_continuing_the_prompt_without_a_number_asks_again(self):
        reply = app._try_deterministic_flow(7, "5491111111111", "no tengo el número a mano", ORDER_PROMPT_HISTORY)
        self.assertEqual(reply, "No pasa nada, decime sólo el número de orden y lo reviso. 😊")

    @patch.object(app, "get_order_status")
    def test_unambiguous_single_message_request_needs_no_prior_context(self, get_status):
        get_status.return_value = {
            "found": True, "order_number": 1234, "status": "open",
            "shipping_status": "pending", "tracking": None, "payment_status": "paid",
        }
        reply = app._try_deterministic_flow(7, "5491111111111", "no me llegó, el número es 1234", [])
        get_status.assert_called_once_with("1234")
        self.assertIn("preparación", reply)

    def test_strong_evidence_without_a_number_asks_for_it(self):
        reply = app._try_deterministic_flow(7, "5491111111111", "no me llegó todavía", [])
        self.assertEqual(reply, app.ORDER_NUMBER_PROMPT_TEXT)

    def test_weak_mention_of_pedido_alone_is_not_treated_as_tracking(self):
        # "mi pedido" alone (no number, no strong phrase) must fall through to
        # the model -- "mi pedido llegó perfecto, gracias" is a compliment,
        # not a status request.
        reply = app._try_deterministic_flow(
            7, "5491111111111", "mi pedido llegó perfecto, gracias", [],
        )
        self.assertIsNone(reply)

    def test_envio_alone_is_never_tracking(self):
        reply = app._try_deterministic_flow(7, "5491111111111", "¿cuánto sería el envío?", [])
        self.assertIsNone(reply)


class RunTrackingLookupTests(unittest.TestCase):
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={"found": False, "message": "No encontré esa orden."})
    def test_order_not_found_escalates_with_context(self, get_status, queue_for_isa):
        reply = app._run_tracking_lookup(7, "5491111111111", "999999", [{"role": "user", "content": "hola"}])

        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "bot_fallback")
        self.assertEqual(queue_for_isa.call_args.kwargs["conversation_context"], [{"role": "user", "content": "hola"}])
        self.assertIn("no me aparece en el sistema", reply)

    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 55, "status": "shipped",
        "shipping_status": "shipped", "tracking": None, "payment_status": "paid",
    })
    def test_shipped_without_tracking_escalates_as_a_contradiction(self, get_status, queue_for_isa):
        reply = app._run_tracking_lookup(7, "5491111111111", "55", [])
        queue_for_isa.assert_called_once()
        self.assertIn("inconsistencia", reply)

    @patch.object(app, "get_order_status", return_value={
        "found": True, "order_number": 77, "status": "open",
        "shipping_status": "shipped", "tracking": "RR1AR", "payment_status": "paid",
    })
    def test_found_with_tracking_never_escalates(self, get_status):
        with patch.object(app, "_queue_for_isa") as queue_for_isa:
            reply = app._run_tracking_lookup(7, "5491111111111", "77", [])
        queue_for_isa.assert_not_called()
        self.assertIn("RR1AR", reply)


class ProductsFlowTests(unittest.TestCase):
    RESULTS = [
        {"product_id": 1, "name": "SHOOW TOOLS - NATURAL SHOOW", "variants": [{"sku": "NATURAL-1", "quantity": 8}]},
        {"product_id": 2, "name": "SHOOW TOOLS - ISABEL I", "variants": [{"sku": "ISABEL-1", "quantity": 3}]},
    ]

    @patch.object(app, "search_available_products")
    def test_menu_selection_1_renders_a_numbered_list(self, search):
        search.return_value = self.RESULTS
        history = [{"role": "assistant", "content": MENU_TEXT}, {"role": "user", "content": "algo natural"}]
        reply = app._try_deterministic_flow(7, "5491111111111", "1", history)

        self.assertIn("1. SHOOW TOOLS - NATURAL SHOOW", reply)
        self.assertIn("2. SHOOW TOOLS - ISABEL I", reply)
        self.assertIn("3. Hablar con Isa", reply)

    @patch.object(app, "search_available_products", return_value=[])
    def test_no_candidates_offers_to_talk_more_or_isa(self, search):
        reply = app._run_products_flow("algo muy raro")
        self.assertIn("Isa", reply)

    @patch.object(app, "save_product_selection")
    @patch.object(app, "get_stock", return_value={
        "found": True, "sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I",
        "status": "in_stock", "price": "30000",
    })
    @patch.object(app, "search_available_products", return_value=[
        {"product_id": 2, "name": "SHOOW TOOLS - ISABEL I", "variants": [{"sku": "ISABEL-1"}]},
    ])
    def test_selecting_a_shown_product_saves_it_as_active(self, search, get_stock, save_selection):
        history = [{"role": "assistant", "content": PRODUCTS_LIST_TEXT}]
        reply = app._try_deterministic_flow(7, "5491111111111", "2", history)

        save_selection.assert_called_once()
        saved_candidate = save_selection.call_args.args[1]
        self.assertEqual(saved_candidate["sku"], "ISABEL-1")
        self.assertIn("SHOOW TOOLS - ISABEL I", reply)

    @patch.object(app, "_queue_for_isa")
    def test_selecting_the_isa_option_from_the_products_list_escalates(self, queue_for_isa):
        history = [{"role": "assistant", "content": PRODUCTS_LIST_TEXT}]
        reply = app._try_deterministic_flow(7, "5491111111111", "3", history)
        queue_for_isa.assert_called_once()
        self.assertIn("Isa", reply)


class PurchaseMenuEntryTests(unittest.TestCase):
    @patch.object(app, "get_product_selection", return_value=None)
    def test_no_active_product_asks_which_one(self, get_selection):
        reply = app._run_purchase_menu_entry(7)
        self.assertIn("qué producto", reply)

    @patch.object(app, "get_stock", return_value={"found": True, "status": "out_of_stock"})
    @patch.object(app, "get_product_selection", return_value={"sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I"})
    def test_out_of_stock_on_revalidation_never_starts_the_intake(self, get_selection, get_stock):
        with patch.object(app, "start_sales_intake") as start_intake:
            reply = app._run_purchase_menu_entry(7)
        start_intake.assert_not_called()
        self.assertIn("ya no tiene stock", reply)

    @patch.object(app, "get_stock", return_value={
        "found": True, "status": "in_stock", "product_name": "SHOOW TOOLS - ISABEL I",
        "variant": "8mm", "price": "30000",
    })
    @patch.object(app, "get_product_selection", return_value={"sku": "ISABEL-1", "product_name": "SHOOW TOOLS - ISABEL I"})
    def test_in_stock_starts_the_existing_sales_intake_machinery(self, get_selection, get_stock):
        with patch.object(app, "start_sales_intake") as start_intake:
            app._run_purchase_menu_entry(7)
        start_intake.assert_called_once()
        self.assertEqual(start_intake.call_args.kwargs["selected_sku"], "ISABEL-1")


class MenuDispatchTests(unittest.TestCase):
    @patch.object(app, "search_available_products", return_value=[])
    def test_selection_1_calls_products_flow(self, search):
        history = [{"role": "assistant", "content": MENU_TEXT}, {"role": "user", "content": "pestañas"}]
        app._try_deterministic_flow(7, "5491111111111", "1", history)
        search.assert_called_once_with("pestañas")

    @patch.object(app, "get_product_selection", return_value=None)
    def test_selection_2_calls_purchase_entry(self, get_selection):
        history = [{"role": "assistant", "content": MENU_TEXT}]
        reply = app._try_deterministic_flow(7, "5491111111111", "2", history)
        get_selection.assert_called_once()
        self.assertIn("qué producto", reply)

    def test_selection_3_asks_for_order_number(self):
        history = [{"role": "assistant", "content": MENU_TEXT}]
        reply = app._try_deterministic_flow(7, "5491111111111", "3", history)
        self.assertEqual(reply, app.ORDER_NUMBER_PROMPT_TEXT)

    @patch.object(app, "_queue_for_isa")
    def test_selection_4_escalates_to_isa(self, queue_for_isa):
        history = [{"role": "assistant", "content": MENU_TEXT}]
        reply = app._try_deterministic_flow(7, "5491111111111", "4", history)
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "human_handoff")
        self.assertIn("Isa", reply)

    def test_no_flow_context_returns_none(self):
        self.assertIsNone(app._try_deterministic_flow(7, "5491111111111", "hola, ¿qué tal?", []))


if __name__ == "__main__":
    unittest.main()
