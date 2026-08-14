"""Deterministic checks for Fred's pre-approval sales flow.

These tests never call Meta, DeepSeek, Tiendanube or Supabase. They protect
the small decisions that must always behave the same way, independently of
how a language model phrases an answer.
"""

import asyncio
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

# Importing app creates a Gemini client, but these tests never call it.
os.environ.setdefault("GEMINI_API_KEY", "test-key")
import app  # noqa: E402
import agent  # noqa: E402
from tiendanube_events import webhook_signature_is_valid  # noqa: E402
import tiendanube_events  # noqa: E402
import tiendanube_tools  # noqa: E402


def _intake(status="confirmation"):
    return {
        "status": status,
        "product_request": "Isabel I chocolate",
        "selected_sku": "ISABEL-CHOC",
        "selected_variant": "8/8/10/12 mm",
        "unit_price": "30000.00",
        "quantity": 4,
        "fulfillment": "shipping",
        "customer_name": "Luis Vera",
        "customer_email": "luis@example.com",
    }


def _in_stock(sku):
    """Default live answer for the fixture SKU: real, sellable, plenty of
    stock. The purchase-integrity invariant verifies every complete draft
    against Tiendanube, so tests must state what the store would say instead
    of reaching the network."""
    return {
        "found": True, "sku": sku, "product_name": "SHOOW TOOLS - ISABEL I (CHOCOLATE)",
        "variant": "8/8/10/12 mm", "status": "in_stock", "quantity": 40,
        "price": "30000.00",
    }


@patch.object(app, "get_stock", _in_stock)
class SalesFlowTests(unittest.TestCase):
    def test_lash_measurements_are_not_mistaken_for_purchase_quantity(self):
        self.assertEqual(app._extract_quantity("Isabel I 8/8/10/12 mm"), 0)
        self.assertEqual(app._extract_quantity("Quiero 3 Isabel I"), 3)
        self.assertEqual(app._extract_quantity("3"), 3)

    @patch.object(app, "update_sales_intake_fields")
    @patch.object(app, "set_sales_intake_customer")
    @patch.object(app, "set_sales_intake_fulfillment")
    @patch.object(app, "set_sales_intake_quantity")
    def test_one_natural_message_completes_all_missing_purchase_fields(
        self, set_quantity, set_fulfillment, set_customer, update_fields
    ):
        intake = _intake(status="fulfillment")
        intake.update({"quantity": None, "fulfillment": None, "customer_name": None, "customer_email": None})
        updated = app._apply_sale_turn_updates(
            11,
            "Quiero 2 unidades, envío. Nombre: María Pérez. Email: maria@example.com",
            intake,
        )

        self.assertTrue(app._sale_is_complete(updated))
        self.assertEqual(updated["quantity"], 2)
        self.assertEqual(updated["fulfillment"], "shipping")
        self.assertEqual(updated["customer_name"], "María Pérez")
        set_quantity.assert_called_once_with(11, 2)
        set_fulfillment.assert_called_once_with(11, "shipping")
        set_customer.assert_called_once_with(11, "María Pérez", "maria@example.com")
        update_fields.assert_called_once_with(11, "confirmation")

    def test_customer_fields_accept_email_without_reasking_for_name(self):
        self.assertEqual(
            app._extract_customer_fields("perdón, mi mail correcto es maria@hotmail.com"),
            {"customer_email": "maria@hotmail.com"},
        )

    @patch.object(app, "update_sales_intake_fields")
    @patch.object(app, "set_sales_intake_quantity")
    def test_latest_explicit_quantity_replaces_prior_quantity(self, set_quantity, update_fields):
        updated = app._apply_sale_turn_updates(
            11, "perdón, son 3", _intake()
        )
        self.assertEqual(updated["quantity"], 3)
        set_quantity.assert_called_once_with(11, 3)
        update_fields.assert_called_once_with(11, "confirmation")

    def test_missing_fulfillment_is_asked_in_words_unless_buttons_are_enabled(self):
        intake = _intake()
        intake["fulfillment"] = None
        with patch.object(app, "FULFILLMENT_BUTTONS_ENABLED", False):
            self.assertIn("envío o retiro", app._sales_missing_step(intake))
        with patch.object(app, "FULFILLMENT_BUTTONS_ENABLED", True):
            self.assertEqual(app._sales_missing_step(intake), "__FULFILLMENT_BUTTONS__")

    def test_confirmation_tolerates_typo_but_not_a_correction_sentence(self):
        self.assertTrue(app._is_sale_confirmation("si confimo"))
        self.assertTrue(app._is_sale_confirmation("confirmo"))
        self.assertFalse(app._is_sale_confirmation("si quiero corregirlo"))

    @patch.dict(os.environ, {"TIENDANUBE_CLIENT_SECRET": "test-secret"})
    def test_tiendanube_webhook_signature_rejects_tampering(self):
        import hashlib
        import hmac

        body = b'{"store_id":"2060155","event":"order/paid","id":123}'
        signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(webhook_signature_is_valid(body, signature))
        self.assertFalse(webhook_signature_is_valid(body + b"x", signature))

    @patch.object(app, "finish_daily_operations_report")
    @patch.object(app, "send_whatsapp_template", return_value=True)
    @patch.object(app, "daily_operations_summary")
    @patch.object(app, "claim_daily_operations_report", return_value=True)
    @patch.object(app, "DAILY_SUMMARY_TEMPLATE_NAME", "resumen_diario")
    @patch.object(app, "DAILY_SUMMARY_ENABLED", True)
    @patch.object(app, "ISA_WHATSAPP_NUMBER", "5491124548738")
    def test_daily_summary_uses_four_values_once_ready(
        self, claim, get_summary, send_template, finish
    ):
        get_summary.return_value = {
            "conversations": 12,
            "approved_checkouts": 3,
            "paid_orders": 2,
            "pending": 1,
        }
        app.run_daily_operations_report(datetime(2026, 8, 9, 21, 0, tzinfo=app.ARGENTINA_TZ))
        send_template.assert_called_once_with(
            "5491124548738", "resumen_diario", app.DAILY_SUMMARY_TEMPLATE_LANGUAGE,
            [12, 3, 2, 1],
        )
        finish.assert_called_once()

    @patch.object(app, "finish_tiendanube_event")
    @patch.object(app, "fred_checkout_for_order")
    @patch.object(app, "fetch_paid_order")
    def test_paid_event_not_owned_by_fred_is_ignored(self, fetch_order, lookup_checkout, finish):
        lookup_checkout.return_value = None
        app._process_tiendanube_paid_order("event-1", "101")
        fetch_order.assert_called_once_with("101")
        finish.assert_called_once_with("event-1", "ignored")

    @patch.object(app, "record_bot_message")
    @patch.object(app, "send_whatsapp_template", return_value=True)
    @patch.object(app, "finish_tiendanube_event")
    @patch.object(app, "fred_checkout_for_order")
    @patch.object(app, "fetch_paid_order")
    @patch.object(app, "PAYMENT_CONFIRMED_TEMPLATE_NAME", "pago_confirmado")
    def test_paid_fred_order_uses_template_not_free_text(
        self, fetch_order, lookup_checkout, finish, send_template, record_message
    ):
        lookup_checkout.return_value = {"customer_phone": "5491111111111", "conversation_id": 7}
        app._process_tiendanube_paid_order("event-2", "102")
        send_template.assert_called_once_with(
            "5491111111111", "pago_confirmado", app.PAYMENT_CONFIRMED_TEMPLATE_LANGUAGE, ["102"]
        )
        record_message.assert_called_once()
        finish.assert_called_once_with("event-2", "processed")

    @patch.object(tiendanube_events, "requests")
    @patch.object(tiendanube_events, "get_tiendanube_configuration")
    def test_existing_payment_webhook_is_never_created_twice(self, get_configuration, requests_mock):
        get_configuration.return_value = {
            "store_id": "2060155", "access_token": "token", "user_agent": "Fred test"
        }
        existing = requests_mock.get.return_value
        existing.json.return_value = [{"id": 9, "event": "order/paid", "url": "https://fred.test/hook"}]
        result = tiendanube_events.register_order_paid_webhook("https://fred.test/hook")
        self.assertEqual(result, {"created": False, "id": 9})
        requests_mock.post.assert_not_called()

    def test_compact_customer_details_excludes_fulfillment_word(self):
        self.assertEqual(
            app._extract_customer_details("envío, Luis Vera, luis@example.com"),
            ("Luis Vera", "luis@example.com"),
        )

    def test_labeled_customer_details_exclude_intro_words(self):
        self.assertEqual(
            app._extract_customer_details(
                "genial te dejo los datos:\nEntrega: envío\nNombre y apellido: Luis Vera\nEmail: luis@example.com"
            ),
            ("Luis Vera", "luis@example.com"),
        )

    def test_unlabeled_intro_words_do_not_become_customer_name(self):
        self.assertEqual(
            app._extract_customer_details(
                "genial te dejo los datos: envio, Luis Vera, luis@example.com"
            ),
            ("Luis Vera", "luis@example.com"),
        )

    def test_natural_customer_intro_does_not_become_customer_name(self):
        self.assertEqual(
            app._extract_customer_details(
                "Hola, te comparto todo: envío, Luis Vera, luis@example.com"
            ),
            ("Luis Vera", "luis@example.com"),
        )

    def test_natural_name_sentence_is_supported(self):
        self.assertEqual(
            app._extract_customer_details(
                "envío, mi nombre es Luis Enrique Vera y mi email es luis@example.com"
            ),
            ("Luis Enrique Vera", "luis@example.com"),
        )

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    def test_service_fallback_is_honest_and_creates_no_case(
        self, queue_for_isa, record_message, send_message
    ):
        # A technical outage tells the truth and leaves Isa's contact; it no
        # longer parks a bot_fallback nobody is watching.
        with patch.object(app, "_queue_for_isa") as queue_for_isa:
            app._send_service_fallback(
                "5491111111111", 11, "¿tienen stock?", [], "Fred no pudo consultar.",
            )
        queue_for_isa.assert_not_called()
        delivered = send_message.call_args.args[1]
        self.assertIn("prefiero no darte un dato incorrecto", delivered)
        self.assertIn(app.isa_contact_number(), delivered)

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "mark_sales_intake_ready")
    def test_confirmo_creates_pending_review_not_a_new_form(
        self, mark_ready, queue_for_isa, record_message, send_message
    ):
        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "confirmo",
            _intake(),
            [{"role": "user", "content": "quiero 4 Isabel I"}],
        )

        self.assertTrue(handled)
        mark_ready.assert_called_once_with(11)
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "purchase_review")
        self.assertIn("ya se lo pasé a Isa", send_message.call_args.args[1])
        record_message.assert_called_once()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "mark_sales_intake_ready")
    def test_numeric_alias_1_confirms_at_the_confirmation_step(
        self, mark_ready, queue_for_isa, record_message, send_message
    ):
        handled = app._handle_sales_intake(11, "5491111111111", "1", _intake(), [])
        self.assertTrue(handled)
        mark_ready.assert_called_once_with(11)
        queue_for_isa.assert_called_once()
        self.assertEqual(queue_for_isa.call_args.args[2], "purchase_review")

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    def test_numeric_alias_2_asks_what_to_modify(self, record_message, send_message):
        handled = app._handle_sales_intake(11, "5491111111111", "2", _intake(), [])
        self.assertTrue(handled)
        self.assertIn("qué querés cambiar", send_message.call_args.args[1])

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "cancel_sales_intake")
    def test_numeric_alias_3_cancels(self, cancel_intake, record_message, send_message):
        handled = app._handle_sales_intake(11, "5491111111111", "3", _intake(), [])
        self.assertTrue(handled)
        cancel_intake.assert_called_once_with(11)
        self.assertIn("cancelé", send_message.call_args.args[1])

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    def test_numeric_alias_4_hands_over_isa_contact_without_touching_the_draft(
        self, queue_for_isa, record_message, send_message
    ):
        # Isa's contact, not a case: no pending is created any more.
        intake = _intake()
        with patch.object(app, "_queue_for_isa") as queue_for_isa:
            handled = app._handle_sales_intake(11, "5491111111111", "4", intake, [])
        self.assertTrue(handled)
        queue_for_isa.assert_not_called()
        self.assertIn(app.isa_contact_number(), send_message.call_args.args[1])

    @patch.object(app, "send_customer_fulfillment_buttons", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "update_sales_intake_fields")
    @patch.object(app, "set_sales_intake_quantity")
    def test_numeric_reply_before_confirmation_step_is_not_a_menu_alias(
        self, set_quantity, update_fields, record_message, send_buttons
    ):
        # "2" during the quantity step means 2 unidades, not "modificar" --
        # the alias must only apply once the summary was actually shown.
        intake = _intake(status="quantity")
        intake.update({"quantity": None, "fulfillment": None, "customer_name": None, "customer_email": None})
        app._handle_sales_intake(11, "5491111111111", "2", intake, [])
        set_quantity.assert_called_once_with(11, 2)

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "_queue_for_isa")
    @patch.object(app, "mark_sales_intake_ready")
    def test_complete_draft_confirms_even_if_legacy_status_is_customer(
        self, mark_ready, queue_for_isa, record_message, send_message
    ):
        """Corrections must not make a completed order request contact data again."""
        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "Sí, confirmo",
            _intake(status="customer"),
            [],
        )

        self.assertTrue(handled)
        mark_ready.assert_called_once_with(11)
        queue_for_isa.assert_called_once()
        self.assertIn("ya se lo pasé a Isa", send_message.call_args.args[1])

    @patch.object(app, "send_whatsapp_text")
    @patch.object(app, "record_bot_message")
    @patch.object(app, "clear_product_selection")
    @patch.object(app, "cancel_sales_intake")
    def test_different_product_releases_unfinished_checkout_before_retrieval(
        self, cancel_intake, clear_selection, record_message, send_message
    ):
        intake = _intake(status="quantity")
        intake.update({
            "quantity": None,
            "fulfillment": None,
            "customer_name": None,
            "customer_email": None,
        })

        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "Me gustaría comprar un perfume de Rare Beauty, ¿tendrán? ¿A qué precio?",
            intake,
            [],
        )

        self.assertFalse(handled)
        cancel_intake.assert_called_once_with(11)
        clear_selection.assert_called_once_with(11)
        send_message.assert_not_called()
        record_message.assert_not_called()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "clear_product_selection")
    @patch.object(app, "cancel_sales_intake")
    def test_same_product_request_keeps_unfinished_checkout(
        self, cancel_intake, clear_selection, record_message, send_message
    ):
        intake = _intake(status="quantity")
        intake.update({
            "quantity": None,
            "fulfillment": None,
            "customer_name": None,
            "customer_email": None,
        })

        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "Quiero comprar las Isabel I chocolate",
            intake,
            [],
        )

        self.assertTrue(handled)
        cancel_intake.assert_not_called()
        clear_selection.assert_not_called()
        # Every missing field is asked for in one natural message now, instead
        # of walking the customer through a four-question wizard.
        asked = send_message.call_args.args[1]
        for expected in ("cuántas unidades", "envío o retiro", "nombre y apellido", "email"):
            self.assertIn(expected, asked)
        record_message.assert_called_once()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "get_active_sales_intake", return_value=_intake())
    @patch.object(app, "set_sales_intake_customer")
    @patch.object(app, "set_sales_intake_fulfillment")
    def test_customer_details_complete_the_real_form(
        self, set_fulfillment, set_customer, get_active, record_message, send_message
    ):
        handled = app._handle_sales_intake(
            11,
            "5491111111111",
            "envío, Luis Vera, luis@example.com",
            _intake(status="fulfillment"),
            [],
        )

        self.assertTrue(handled)
        # The same-message parser now preserves already-known values instead
        # of rewriting them just because the customer repeated the details.
        set_fulfillment.assert_not_called()
        set_customer.assert_not_called()
        self.assertIn("Subtotal de productos: $120.000", send_message.call_args.args[1])
        record_message.assert_called_once()
        get_active.assert_not_called()

    @patch.object(app, "get_active_sales_intake")
    @patch.object(app, "set_sales_intake_customer")
    @patch.object(app, "set_sales_intake_fulfillment")
    def test_same_purchase_message_keeps_delivery_and_contact_details(
        self, set_fulfillment, set_customer, get_active
    ):
        intake = _intake(status="customer")
        get_active.side_effect = [intake, intake]

        summary = app._apply_sale_details_from_same_message(
            11,
            "Quiero 4 packs de 10 pares\n"
            "Entrega: envío\n"
            "Nombre: Luis Vera\n"
            "Email: luis@example.com",
        )

        set_fulfillment.assert_not_called()
        set_customer.assert_not_called()
        self.assertIn("Cantidad: 4", summary)
        self.assertIn("Nombre: Luis Vera", summary)

    def test_1000_purchase_guard_variations(self):
        """500 purchase variations + 500 non-purchase variations.

        This is deliberately a deterministic stress test of the guard, not a
        claim that a generative model can be certified by 1,000 fake chats.

        Uses a context manager rather than a method decorator on purpose: the
        class-level get_stock default would otherwise win over a same-target
        method decorator and the call count would never be exercised.
        """
        with patch.object(app, "get_stock") as get_stock:
            get_stock.side_effect = lambda sku: {
                "status": "in_stock",
                "sku": sku,
                "product_name": "Modelo de prueba",
                "variant": "Única",
                "price": "1000.00",
            }

            for index in range(500):
                result = {"tool_calls": [{"name": "get_stock", "arguments": {"sku": "SKU-{}".format(index)}}]}
                candidate = app._verified_purchase_candidate_from_tool_calls(
                    "quiero comprar el modelo {}".format(index), result
                )
                self.assertEqual(candidate["sku"], "SKU-{}".format(index))

                no_purchase = app._verified_purchase_candidate_from_tool_calls(
                    "quiero ver el modelo {}".format(index), result
                )
                self.assertEqual(no_purchase, {})

            self.assertEqual(get_stock.call_count, 500)

    @patch.object(app, "get_stock")
    def test_ambiguous_multiple_skus_never_starts_a_sale(self, get_stock):
        candidate = app._verified_purchase_candidate_from_tool_calls(
            "quiero comprar pestañas naturales",
            {
                "tool_calls": [
                    {"name": "get_stock", "arguments": {"sku": "A"}},
                    {"name": "get_stock", "arguments": {"sku": "B"}},
                ]
            },
        )
        self.assertEqual(candidate, {})
        get_stock.assert_not_called()

    def test_encargo_context_is_never_a_normal_checkout(self):
        self.assertTrue(
            app._is_special_sale_context(
                "sí porfa",
                [
                    {
                        "role": "assistant",
                        "content": "Como es un encargo, Isa confirma las condiciones.",
                    }
                ],
            )
        )
        self.assertTrue(app._is_special_sale_context("quiero encargar un labial", []))
        self.assertFalse(app._is_special_sale_context("quiero comprar 2 Isabel I", []))

    def test_old_encargo_history_does_not_block_a_new_normal_purchase(self):
        history = [
            {"role": "assistant", "content": "Te envié las condiciones del encargo."},
            {"role": "user", "content": "gracias"},
            {"role": "assistant", "content": "¡De nada!"},
        ]
        self.assertFalse(app._is_special_sale_context("quiero comprar 2 Isabel I", history))

    def test_isa_can_request_encargo_conditions_in_plain_language(self):
        self.assertTrue(
            app._is_special_conditions_request(
                "Necesito que le envíes las condiciones de las ventas por encargo"
            )
        )
        self.assertFalse(app._is_special_conditions_request("enviá el link de pago"))


class AgentOutputSafetyTests(unittest.TestCase):
    @patch.object(app, "FRED_BETA_ALLOWED_PHONES", {"5491111111111"})
    @patch.object(app, "FRED_CUSTOMER_MODE", "allowlist")
    def test_allowlist_blocks_unknown_phone_without_ai(self):
        self.assertEqual(app._customer_access_reply("5491111111111"), "")
        self.assertIn("terminando de habilitar", app._customer_access_reply("5491222222222"))

    @patch.object(app, "FRED_CUSTOMER_MODE", "paused")
    def test_paused_mode_never_reaches_agent(self):
        self.assertIn("ajuste breve", app._customer_access_reply("5491111111111"))

    def test_social_messages_do_not_need_an_ai_call(self):
        self.assertEqual(app._simple_customer_reply("hola"), "¡Hola! 😊 ¿En qué te puedo ayudar?")
        self.assertIn("De nada", app._simple_customer_reply("muchas gracias"))
        self.assertEqual(app._simple_customer_reply("Hola, busco pestañas"), "")

    def test_agent_has_a_bounded_number_of_model_rounds(self):
        self.assertLessEqual(agent.MAX_TOOL_ROUNDS, 5)

    @patch.object(agent, "_ask_deepseek")
    def test_agent_reports_usage_without_sending_an_extra_call(self, ask_deepseek):
        ask_deepseek.return_value = {
            "content": "Te ayudo con eso 😊",
            "_fred_usage": {"prompt_tokens": 120, "completion_tokens": 15, "total_tokens": 135},
        }
        result = agent.answer("consulta", history=[])
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 135)
        ask_deepseek.assert_called_once()

    def test_unverified_link_is_removed(self):
        self.assertEqual(
            agent._remove_unverified_urls("Mirá https://inventado.test/producto", []),
            "Mirá",
        )

    def test_catalog_markdown_is_flattened_for_whatsapp(self):
        self.assertEqual(
            agent._plain_whatsapp_text("**Isabel I**"),
            "Isabel I",
        )

    @patch.object(tiendanube_tools, "_get")
    def test_generic_recommendation_excludes_surprise_sets(self, get_products):
        get_products.return_value = [
            {
                "id": 1,
                "published": True,
                "name": {"es": "Set de pestañas sorpresa"},
                "variants": [{"sku": "SURPRISE", "stock": 9, "values": []}],
            },
            {
                "id": 2,
                "published": True,
                "name": {"es": "Isabel I Chocolate"},
                "variants": [{"sku": "ISABEL", "stock": 3, "values": []}],
            },
        ]
        results = tiendanube_tools.search_available_products("pestañas naturales")
        self.assertEqual([item["name"] for item in results], ["Isabel I Chocolate"])

    @patch.object(tiendanube_tools, "_get")
    def test_catalog_audit_is_read_only_and_finds_sellability_risks(self, get_products):
        get_products.side_effect = [
            [
                {
                    "published": True,
                    "name": {"es": "Producto publicado"},
                    "variants": [
                        {"sku": "", "stock": 3, "values": []},
                        {"sku": "DUP", "stock": None, "values": []},
                    ],
                },
                {
                    "published": False,
                    "name": {"es": "Producto oculto"},
                    "variants": [{"sku": "DUP", "stock": 2, "values": []}],
                },
            ],
            [],
        ]
        audit = tiendanube_tools.catalog_health_audit()
        self.assertEqual(audit["totals"]["published_without_sku"], 1)
        self.assertEqual(audit["totals"]["published_untracked_stock"], 1)
        self.assertEqual(audit["totals"]["hidden_with_positive_stock"], 1)
        self.assertEqual(audit["totals"]["duplicate_skus"], 1)
        self.assertTrue(get_products.called)


class QualityReviewTests(unittest.TestCase):
    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "daily_quality_snapshot")
    def test_isa_can_request_quality_snapshot_without_ai(self, snapshot, send_message):
        snapshot.return_value = {
            "pending_actions": 2,
            "bot_fallbacks_today": 1,
            "human_handoffs_today": 1,
            "special_sales_today": 1,
            "pending_purchase_reviews": 1,
        }
        self.assertTrue(app._handle_isa_quality_review_request("calidad"))
        self.assertIn("Casos donde Fred pidió ayuda: 1", send_message.call_args.args[1])
        snapshot.assert_called_once()


class DashboardSafetyTests(unittest.TestCase):
    @patch.object(app, "agent_observability_snapshot")
    @patch.object(app, "dashboard_snapshot")
    def test_dashboard_exposes_read_only_catalog_audit(self, snapshot, observability):
        snapshot.return_value = {
            "last_24h": {
                "active_conversations": 0,
                "customer_messages": 0,
                "fred_messages": 0,
                "pending_actions": 0,
                "approved_checkouts": 0,
                "fred_paid_orders": 0,
            },
            "pending_by_type": {},
            "conversations": [],
        }
        observability.return_value = {
            "turns": 8,
            "average_duration_ms": 950,
            "average_tokens": 120,
            "service_fallbacks": 1,
            "actions": {"reply": 5, "handoff_to_isa": 2},
        }
        request = type("Request", (), {"query_params": {}})()
        response = asyncio.run(app.operations_dashboard(request, username="isa"))
        page = response.body.decode("utf-8")
        self.assertIn("Auditar catálogo", page)
        self.assertIn("Calidad y rendimiento de Fred", page)
        self.assertIn("Respuestas resueltas: 5", page)
        self.assertIn("950 ms", page)


class IsaInternalSaleFlowTests(unittest.TestCase):
    @patch.object(app, "list_pending_actions", return_value=[])
    @patch.object(app, "pending_action_count", return_value=False)
    @patch.object(app, "record_bot_message")
    @patch.object(app, "set_conversation_state")
    @patch.object(app, "resolve_pending_action")
    @patch.object(app, "send_whatsapp_text", return_value=True)
    def test_cancelling_purchase_review_notifies_customer_and_returns_to_fred(
        self, send_message, resolve_action, set_state, record_message, pending_count, list_pending
    ):
        resolve_action.return_value = {
            "conversation_id": 7,
            "action_type": "purchase_review",
            "payload": {"customer_phone": "5491111111111"},
        }

        app.handle_isa_message("", button_reply_id="reject:11")

        resolve_action.assert_called_once_with(11, "rejected")
        set_state.assert_called_once_with(7, "BOT")
        customer_messages = [call.args for call in send_message.call_args_list if call.args[0] == "5491111111111"]
        self.assertEqual(len(customer_messages), 1)
        self.assertIn("No tenés que pagar nada", customer_messages[0][1])
        self.assertIn("seguimos viendo alternativas", customer_messages[0][1])
        record_message.assert_called_once_with(7, customer_messages[0][1])
        self.assertIn("Fred ya le avisó", send_message.call_args_list[-1].args[1])
        pending_count.assert_called_once()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "wait_for_isa_response", return_value=True)
    @patch.object(
        app,
        "_pending_action_by_id",
        return_value={"id": 31, "action_type": "bot_fallback"},
    )
    def test_isa_can_reply_to_fred_with_a_reviewed_answer(
        self, get_pending, wait_for_reply, send_message
    ):
        app.handle_isa_message("", button_reply_id="reply_to_fred:31")
        wait_for_reply.assert_called_once_with(31)
        self.assertIn("Escribime la respuesta", send_message.call_args.args[1])

    @patch.object(app, "send_isa_sale_type_menu", return_value=True)
    @patch.object(app, "start_isa_sale_session")
    @patch.object(app, "get_isa_sale_session", return_value=None)
    @patch.object(app, "list_pending_actions", return_value=[])
    def test_natural_external_sale_request_opens_category_menu(
        self, list_pending, get_session, start_session, send_menu
    ):
        app.handle_isa_message("Vendí unos productos por Instagram, ¿me armás el link?")
        start_session.assert_called_once_with(app.ISA_WHATSAPP_NUMBER)
        send_menu.assert_called_once()
        get_session.assert_called_once_with(app.ISA_WHATSAPP_NUMBER)

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "set_isa_sale_session_type")
    def test_isa_can_choose_encargo_without_writing_a_command(self, set_type, send_message):
        handled = app._handle_isa_sale_session("Encargo", "sale_type:encargo")
        self.assertTrue(handled)
        set_type.assert_called_once_with(app.ISA_WHATSAPP_NUMBER, "encargo")
        self.assertIn("Encargo", send_message.call_args.args[1])

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "add_isa_sale_session_details")
    @patch.object(
        app,
        "get_isa_sale_session",
        return_value={"status": "collect_details", "sale_type": "venta_mayorista", "details": None},
    )
    def test_selected_type_keeps_details_for_review(self, get_session, add_details, send_message):
        handled = app._handle_isa_sale_session(
            "Isabel I chocolate x 12, Cliente Ejemplo, cliente@example.com", ""
        )
        self.assertTrue(handled)
        add_details.assert_called_once()
        self.assertIn("Venta mayorista", send_message.call_args.args[1])

    @patch.object(
        app,
        "get_isa_sale_session",
        return_value={"status": "review", "sale_type": "encargo", "details": "algo"},
    )
    def test_customer_approval_button_is_not_captured_by_internal_draft(self, get_session):
        self.assertFalse(app._handle_isa_sale_session("Tomar caso", "approve:17"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
