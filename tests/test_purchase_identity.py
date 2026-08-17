"""A SKU is never chosen by similarity.

The commercial rule these tests encode, in full:

  Identity is confirmed by three things together -- the customer named the
  product, it matches a real catalog product, and live stock confirms it.

  A) exactly one unambiguous SKU -> pin it, show it, offer [Comprar].
  B) several possible SKUs       -> choose nothing, offer no button, ask which.
  C) nothing confirmed           -> offer nothing found by similarity, ask.

The concrete production failure behind (B): "Quiero comprar Isabel I" names a
product the store carries under several distinct names, Fred could not resolve
it to one SKU, and so no [Comprar] button ever appeared -- the purchase flow
was simply unreachable. The fix is not to pick one; it is to ask, with the
store's real options, and keep the button for the case that deserves it.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import app  # noqa: E402


# Real shapes, taken from the live store: several distinct products all called
# "Isabel I", which is exactly why a name alone can never be a purchase.
ISABEL_PRODUCTS = [
    {
        "product_id": 1, "name": "SHOOW TOOLS - ISABEL I",
        "variants": [
            {"sku": "ISABEL-8-10", "description": "8/10 mm", "quantity": 4},
            {"sku": "ISABEL-MIX", "description": "Mixed", "quantity": 2},
        ],
    },
    {
        "product_id": 2, "name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
        "variants": [{"sku": "ISABEL-CHOCO", "description": "8/10 mm", "quantity": 3}],
    },
]

# A brand, not a product: every match is "Rare Beauty - <something else>".
RARE_BEAUTY_PRODUCTS = [
    {
        "product_id": 10, "name": "Rare Beauty - Soft Pinch Liquid Blush (Lucky)",
        "variants": [{"sku": "RB-BLUSH", "description": "Lucky", "quantity": 2}],
    },
    {
        "product_id": 11, "name": "Rare beauty - Find Comfort Pouch",
        "variants": [{"sku": "RB-POUCH", "description": "", "quantity": 1}],
    },
]

ONE_UNAMBIGUOUS_PRODUCT = [
    {
        "product_id": 20, "name": "SHOOW TOOLS - NATURAL SHOOW",
        "variants": [{"sku": "NATURAL-1", "description": "Única", "quantity": 5}],
    },
]


def _search_returning(products):
    return lambda query, limit=5: list(products)


class PurchaseIdentityTests(unittest.TestCase):
    """_purchase_identity_from_message: the whole rule, in one function."""

    def _identity(self, message, products, stock=None):
        stock = stock or {}
        with patch.object(app, "search_available_products", _search_returning(products)), \
                patch.object(app, "get_stock", lambda sku: stock.get(sku, {"found": False})):
            return app._purchase_identity_from_message("", message, "")

    # --- case A: one unambiguous SKU -------------------------------------

    def test_one_named_product_with_one_variant_resolves_to_that_sku(self):
        identity = self._identity(
            "quiero comprar natural shoow",
            ONE_UNAMBIGUOUS_PRODUCT,
            stock={"NATURAL-1": {
                "found": True, "sku": "NATURAL-1", "status": "in_stock",
                "product_name": "SHOOW TOOLS - NATURAL SHOOW", "variant": "Única",
                "price": "36000",
            }},
        )
        self.assertEqual(identity["status"], "resolved")
        self.assertEqual(identity["candidate"]["sku"], "NATURAL-1")
        self.assertEqual(identity["candidate"]["unit_price"], "36000")

    def test_a_single_match_the_customer_never_named_is_not_identity(self):
        # The live search is generous and can return one thing for an
        # unrelated word. One result is not the same as one answer.
        identity = self._identity(
            "quiero comprar algo lindo para regalar", ONE_UNAMBIGUOUS_PRODUCT,
        )
        self.assertEqual(identity["status"], "unknown")

    def test_a_named_product_that_is_not_in_stock_is_not_identity(self):
        identity = self._identity(
            "quiero comprar natural shoow",
            ONE_UNAMBIGUOUS_PRODUCT,
            stock={"NATURAL-1": {"found": True, "sku": "NATURAL-1", "status": "out_of_stock"}},
        )
        self.assertEqual(identity["status"], "unknown")

    def test_live_verification_failure_never_becomes_a_purchase(self):
        with patch.object(app, "search_available_products", _search_returning(ONE_UNAMBIGUOUS_PRODUCT)), \
                patch.object(app, "get_stock", side_effect=RuntimeError("tiendanube caída")):
            identity = app._purchase_identity_from_message("", "quiero comprar natural shoow", "")
        self.assertEqual(identity["status"], "unknown")

    # --- case B: several possible SKUs -----------------------------------

    def test_isabel_i_is_ambiguous_and_never_picks_a_sku(self):
        identity = self._identity("quiero comprar Isabel I", ISABEL_PRODUCTS)

        self.assertEqual(identity["status"], "ambiguous")
        self.assertNotIn("candidate", identity)
        self.assertEqual(identity["label"].lower(), "isabel i")
        # Every real sellable option, flattened: two variants of one product
        # plus the chocolate one. None of them is marked as chosen.
        self.assertEqual(
            {option["sku"] for option in identity["options"]},
            {"ISABEL-8-10", "ISABEL-MIX", "ISABEL-CHOCO"},
        )

    def test_the_variant_question_lists_real_options_and_asks(self):
        identity = self._identity("quiero comprar Isabel I", ISABEL_PRODUCTS)
        question = app._render_variant_question(identity)

        self.assertIn("Isabel I".lower(), question.lower())
        self.assertIn("¿Cuál buscabas?", question)
        self.assertIn("8/10 mm", question)
        self.assertIn("Mixed", question)
        # The store's brand prefix is presentation noise, and no price or
        # stock number is invented for options nobody verified individually.
        self.assertNotIn("SHOOW TOOLS -", question)
        self.assertNotIn("$", question)

    def test_a_brand_shared_by_different_products_is_not_a_named_product(self):
        # "un perfume de Rare Beauty": the words match five real products, but
        # no product is CALLED "Rare Beauty" -- it is a brand. Asking "¿cuál de
        # estas cinco?" would be offering another category, so this stays a
        # normal conversation instead.
        identity = self._identity(
            "Me gustaría comprar un perfume de Rare Beauty, ¿tendrán?",
            RARE_BEAUTY_PRODUCTS,
        )
        self.assertEqual(identity["status"], "unknown")

    def test_a_shared_name_the_customer_never_said_is_not_ambiguity(self):
        identity = self._identity("quiero comprar algo para mis clientas", ISABEL_PRODUCTS)
        self.assertEqual(identity["status"], "unknown")

    def test_preorders_are_never_offered_as_a_normal_purchase(self):
        preorder_only = [{
            "product_id": 3,
            "name": "PRE VENTA - SHOOW TOOLS - ISABEL I (PESTAÑAS CLUSTER) MIXED",
            "variants": [{"sku": "ISABEL-PRE", "description": "Mixed", "quantity": 5}],
        }]
        identity = self._identity("quiero comprar Isabel I", preorder_only)
        self.assertEqual(identity["status"], "unknown")

    def test_a_preorder_never_appears_among_the_options_of_a_real_one(self):
        with_preorder = ISABEL_PRODUCTS + [{
            "product_id": 3, "name": "PRE VENTA - SHOOW TOOLS - ISABEL I MIXED",
            "variants": [{"sku": "ISABEL-PRE", "description": "Mixed", "quantity": 5}],
        }]
        identity = self._identity("quiero comprar Isabel I", with_preorder)
        self.assertEqual(identity["status"], "ambiguous")
        self.assertNotIn("ISABEL-PRE", {option["sku"] for option in identity["options"]})

    # --- case C: nothing confirmed ---------------------------------------

    def test_nothing_found_is_unknown_and_stays_silent(self):
        self.assertEqual(self._identity("quiero comprar Isabel I", [])["status"], "unknown")


class SharedLabelTests(unittest.TestCase):
    def test_the_label_is_the_common_start_of_the_real_catalog_names(self):
        self.assertEqual(
            app._shared_product_label([
                "SHOOW TOOLS - ISABEL I",
                "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
                "SHOOW TOOLS - ISABEL I (8/10MM)",
            ]).lower(),
            "isabel i",
        )

    def test_names_with_nothing_in_common_produce_no_label(self):
        self.assertEqual(
            app._shared_product_label(["SHOOW TOOLS - FOXY #1", "Jcat beauty - Base"]), "",
        )

    def test_a_label_too_short_to_identify_anything_is_discarded(self):
        self.assertEqual(app._shared_product_label(["AB uno", "AB dos"]), "")


def _default_fred_core_state(conversation_id):
    return {
        "mode": "CHAT", "active_product_id": None, "active_product_name": None,
        "active_sku": None, "active_variant": None, "unit_price": None,
        "quantity": None, "delivery_method": None, "customer_name": None,
        "customer_email": None, "postal_code": None, "checkout_step": None,
        "order_number": None,
    }


class IncomingRequest:
    def __init__(self, phone, text, message_id="wamid-purchase"):
        self._body = {"entry": [{"changes": [{"value": {"messages": [
            {"from": phone, "id": message_id, "text": {"body": text}},
        ]}}]}]}

    async def json(self):
        return self._body


@patch.object(app, "CONVERSATION_DEBOUNCE_SECONDS", 0)
@patch.object(app, "get_fred_core_state", _default_fred_core_state)
@patch.object(app, "reset_fred_core_checkout", lambda conversation_id: None)
@patch.object(app, "get_active_sales_intake", lambda conversation_id: None)
@patch.object(app, "SALES_INTAKE_ENABLED", True)
@patch.object(app, "KNOWLEDGE_RAG_ENABLED", False)
class PurchaseTurnEndToEndTests(unittest.TestCase):
    """The same rule, seen from the WhatsApp turn the customer actually has."""

    PHONE = "5491111111111"

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_a_purchase_of_a_named_product_goes_to_isa_with_its_context(
        self, history, inbound, send_message, record_message, record_turn
    ):
        # Scope change: asking WHICH Isabel I was a step towards selling one.
        # Fred no longer sells, so the whole exchange goes to Isa with what the
        # customer already said -- she picks the variant with them.
        saved = []
        with patch.object(app, "save_fred_core_state", lambda cid, **kw: saved.append(kw)), \
                patch.object(app, "search_available_products", _search_returning(ISABEL_PRODUCTS)), \
                patch.object(app, "get_stock", lambda sku: {"found": False}), \
                patch.object(app, "_live_candidate_context", return_value=""), \
                patch.object(app, "search_similar_products", return_value=""), \
                patch.object(app, "get_product_selection", return_value=None), \
                patch.object(app, "send_customer_action_buttons") as buttons, \
                patch.object(app, "_start_sales_intake") as start_intake, \
                patch.object(app, "answer") as ask_model:
            response = asyncio.run(app.webhook_post(
                IncomingRequest(self.PHONE, "Quiero comprar Isabel I", "wamid-ambiguous")
            ))

        self.assertEqual(response.status_code, 200)
        delivered = send_message.call_args.args[1]
        self.assertIn("Isa", delivered)
        # What the customer already said travels with them.
        self.assertIn("Isabel I", delivered)
        ask_model.assert_not_called()
        buttons.assert_not_called()
        start_intake.assert_not_called()
        self.assertFalse([f for f in saved if f.get("active_sku")])
        for sku in ("ISABEL-8-10", "ISABEL-MIX", "ISABEL-CHOCO"):
            self.assertNotIn(sku, delivered)

    @patch.object(app, "record_agent_turn")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_inbound_message", return_value=(7, "BOT", False))
    @patch.object(app, "load_history", return_value=[])
    @patch.object(app, "BOT_RESPONSE_MODE", "agent")
    def test_an_unambiguous_purchase_goes_to_isa_with_no_button_and_no_checkout(
        self, history, inbound, send_message, record_message, record_turn
    ):
        # Scope change: Fred no longer sells. A purchase Fred could once pin to
        # a SKU and put behind [Comprar] is now handed to Isa -- and no button,
        # checkout or payment link may appear on the way.
        saved = []
        with patch.object(app, "save_fred_core_state", lambda cid, **kw: saved.append(kw)), \
                patch.object(app, "search_available_products", _search_returning(ISABEL_PRODUCTS)), \
                patch.object(app, "get_stock", lambda sku: {"found": False}), \
                patch.object(app, "_live_candidate_context", return_value=""), \
                patch.object(app, "search_similar_products", return_value=""), \
                patch.object(app, "get_product_selection", return_value=None), \
                patch.object(app, "send_customer_action_buttons", return_value=True) as buttons, \
                patch.object(app, "_start_sales_intake") as start_intake, \
                patch.object(app, "answer") as ask_model:
            response = asyncio.run(app.webhook_post(
                IncomingRequest(self.PHONE, "Quiero comprar 2 Isabel I", "wamid-single")
            ))

        self.assertEqual(response.status_code, 200)
        delivered = send_message.call_args.args[1]
        self.assertIn("Isa", delivered)
        buttons.assert_not_called()
        start_intake.assert_not_called()
        ask_model.assert_not_called()
        self.assertFalse([f for f in saved if f.get("active_sku")])


if __name__ == "__main__":
    unittest.main()
