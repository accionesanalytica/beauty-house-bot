"""
FastAPI webhook para Meta WhatsApp Cloud API.
Escucha mensajes, registra su historial en Supabase y, por ahora,
responde con una plantilla de prueba aprobada por Meta.
"""

import asyncio
import contextlib
import html
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from urllib.parse import parse_qs
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Agregar el directorio actual al path para importar agent.py
sys.path.insert(0, os.path.dirname(__file__))

import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import numpy as np
from google import genai
from google.genai import types

from agent import (
    RECOMMENDATIONS_ENABLED,
    answer,
    _collect_turn_candidates,
    _filter_relevant_candidates,
    _format_price,
)
from catalog_rag import (
    format_catalog_context,
    fuse_catalog_candidates,
    lexical_catalog_query,
)
from knowledge_rag import (
    DEFAULT_KNOWLEDGE_TOP_K,
    KnowledgeRetrieval,
    _normalise as _knowledge_normalise,
    _TRACKING_EVIDENCE_RE,
    approved_knowledge_rows,
    build_knowledge_retrieval,
    extract_order_number,
    load_knowledge_chunks,
    retrieve_local_knowledge,
    retrieve_with_recent_context,
    enforce_knowledge_obligations,
    extract_https_urls,
)
from dynamic_checks import (
    DynamicCheckOutcome,
    execute_dynamic_requirements,
    format_dynamic_check_context,
)
from conversation_quality import apply_conversation_contract
from durable_worker import DeliveryContext, current_delivery_context
from message_queue import extract_inbound_messages, ordered_turn_text
from routing_policy import (
    DATA_KNOWLEDGE_ONLY,
    _ANAPHORIC_REFERENCE_RE,
    _UNAMBIGUOUS_PURCHASE_VERB_RE,
    INTENT_ADVICE_REQUEST,
    INTENT_POLICY_QUESTION,
    INTENT_PURCHASE_INTENT,
    _carries_commercial_object,
    _named_catalog_product,
    _pickup_of_a_specific_order,
    order_number_reference,
    align_reply_with_routing,
    build_product_lexicon,
    classify_turn_data_requirement,
    legacy_special_sale_context,
    lifting_clarification_reply,
    resolve_harness_routing,
    visible_routing_contract,
    _order_status_needs_isa,
)
from tiendanube_checkout import CheckoutError, checkout_enabled, create_approved_checkout
from tiendanube_draft_orders import DraftOrderDemoError, create_demo_draft_order
from tiendanube_tools import (
    catalog_health_audit,
    get_order_status,
    get_product_availability,
    get_stock,
    search_available_products,
)
from tiendanube_credentials import (
    TiendanubeCredentialError,
    save_tiendanube_credential,
)
from tiendanube_events import (
    fetch_paid_order,
    register_order_paid_webhook,
    webhook_signature_is_valid,
)
from operations_store import (
    agent_observability_snapshot,
    claim_daily_operations_report,
    daily_quality_snapshot,
    claim_tiendanube_event,
    daily_operations_summary,
    dashboard_conversation,
    dashboard_snapshot,
    finish_daily_operations_report,
    finish_tiendanube_event,
    fred_checkout_for_order,
    record_agent_turn,
)
from conversation_store import (
    add_isa_sale_session_details,
    cancel_sales_intake,
    claim_daily_isa_reminder,
    claim_requested_isa_reminder,
    claim_next_conversation,
    clear_product_selection,
    clear_isa_reminder_snooze,
    clear_isa_sale_session,
    create_pending_action,
    get_fred_core_state,
    get_isa_sale_session,
    get_active_sales_intake,
    get_product_selection,
    is_latest_customer_message,
    isa_reminders_snoozed,
    load_history,
    load_open_customer_turn,
    list_pending_actions,
    mark_sales_intake_ready,
    pending_action_count,
    pending_reminder_snapshot,
    record_isa_feedback,
    record_bot_message,
    record_inbound_message,
    enqueue_inbound_message,
    finish_processing_claim,
    processing_claim_is_current,
    release_processing_claim,
    renew_processing_claim,
    resolve_pending_action,
    release_daily_isa_reminder,
    reset_fred_core_checkout,
    save_fred_core_state,
    save_pending_action_checkout,
    save_pending_action_resolution,
    save_product_selection,
    set_sales_intake_customer,
    set_sales_intake_fulfillment,
    set_sales_intake_product,
    set_sales_intake_quantity,
    update_sales_intake_fields,
    set_conversation_state,
    set_isa_sale_session_type,
    start_isa_sale_session,
    start_sales_intake,
    snooze_isa_reminders,
    wait_for_isa_response,
    set_isa_awaiting,
    clear_isa_awaiting,
)

load_dotenv()

app = FastAPI()
dashboard_security = HTTPBasic(auto_error=False)

# ============================================================
# CONFIGURACIÓN
# ============================================================

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_WEBHOOK_VERIFY_TOKEN = os.getenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN")
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
KNOWLEDGE_RAG_ENABLED = os.getenv("KNOWLEDGE_RAG_ENABLED", "false").lower() == "true"
KNOWLEDGE_RAG_SOURCE = os.getenv("KNOWLEDGE_RAG_SOURCE", "local").strip().lower()
if KNOWLEDGE_RAG_SOURCE not in {"local", "supabase"}:
    print("[Knowledge] Fuente inválida; se usa local como fallback seguro.")
    KNOWLEDGE_RAG_SOURCE = "local"
KNOWLEDGE_DIRECTORY = Path(__file__).resolve().parents[1] / "knowledge"

# Seguridad: hasta completar las pruebas, el webhook conserva la plantilla
# actual. El modo agent se habilitará explícitamente en una etapa posterior.
BOT_RESPONSE_MODE = os.getenv("BOT_RESPONSE_MODE", "template").lower()
# Opening Fred can be gradual without editing code. "open" preserves normal
# production behavior; "allowlist" answers only listed test phones; "paused"
# gives a neutral maintenance reply and never calls the agent.
FRED_CUSTOMER_MODE = os.getenv("FRED_CUSTOMER_MODE", "open").strip().lower()
FRED_BETA_ALLOWED_PHONES = {
    re.sub(r"\D", "", phone)
    for phone in os.getenv("FRED_BETA_ALLOWED_PHONES", "").split(",")
    if re.sub(r"\D", "", phone)
}
ISA_WHATSAPP_NUMBER = os.getenv("ISA_WHATSAPP_NUMBER", "")
SALES_INTAKE_ENABLED = os.getenv("SALES_INTAKE_ENABLED", "false").lower() == "true"
# The two-button envío/retiro screen is a shortcut, not a requirement: Fred
# understands "envío"/"retiro" written naturally, and the checkout never
# depends on the buttons being pressed. Off by default so the flow reads as a
# conversation; set to "true" to bring the shortcut back.
FULFILLMENT_BUTTONS_ENABLED = os.getenv("FULFILLMENT_BUTTONS_ENABLED", "false").lower() == "true"
# A short pause lets WhatsApp users finish a natural burst ("hola" / "quiero
# dos" / "retiro") before Fred decides. The database decides which event is
# newest, so this remains safe if Railway later has more than one instance.
try:
    CONVERSATION_DEBOUNCE_SECONDS = max(
        0.0, min(float(os.getenv("CONVERSATION_DEBOUNCE_SECONDS", "1.5")), 4.0)
    )
except ValueError:
    CONVERSATION_DEBOUNCE_SECONDS = 1.5
# M1 is opt-in until its SQL migration is applied.  The legacy path remains a
# rollback switch; enabling this flag makes the webhook ingestion-only and a
# Postgres-leased worker owns customer replies.
DURABLE_MESSAGE_PROCESSING_ENABLED = (
    os.getenv("DURABLE_MESSAGE_PROCESSING_ENABLED", "false").lower() == "true"
)
try:
    MESSAGE_QUIET_WINDOW_SECONDS = max(
        0.0, float(os.getenv("MESSAGE_QUIET_WINDOW_SECONDS", "1.5"))
    )
    MESSAGE_MAX_BURST_WAIT_SECONDS = max(
        MESSAGE_QUIET_WINDOW_SECONDS,
        float(os.getenv("MESSAGE_MAX_BURST_WAIT_SECONDS", "5.0")),
    )
    MESSAGE_LEASE_SECONDS = max(
        15.0, float(os.getenv("MESSAGE_LEASE_SECONDS", "120"))
    )
    MESSAGE_WORKER_POLL_SECONDS = max(
        0.05, float(os.getenv("MESSAGE_WORKER_POLL_SECONDS", "0.25"))
    )
except ValueError:
    MESSAGE_QUIET_WINDOW_SECONDS = 1.5
    MESSAGE_MAX_BURST_WAIT_SECONDS = 5.0
    MESSAGE_LEASE_SECONDS = 120.0
    MESSAGE_WORKER_POLL_SECONDS = 0.25
# Segunda llave explícita: incluso si existen credenciales demo, una aprobación
# normal nunca crea nada. Solo sirve para probar el recorrido completo.
DEMO_APPROVALS_ENABLED = (
    os.getenv("DEMO_APPROVALS_ENABLED", "false").lower() == "true"
    and os.getenv("TIENDANUBE_DRAFT_ORDERS_MODE", "disabled").lower() == "demo"
)
LIVE_CHECKOUTS_ENABLED = checkout_enabled()
# Los recordatorios sólo pueden funcionar con una plantilla realmente
# aprobada en el WABA que envía. Mientras escalacion_isa no exista ahí, cada
# intento es un error garantizado en los logs, así que quedan apagados salvo
# que se habiliten explícitamente Y exista el nombre de plantilla.
ISA_REMINDERS_ENABLED = (
    os.getenv("ISA_REMINDERS_ENABLED", "false").lower() == "true"
    and os.getenv("ESCALACION_ISA_TEMPLATE_VERIFIED", "false").lower() == "true"
)
ADMIN_DASHBOARD_USERNAME = os.getenv("ADMIN_DASHBOARD_USERNAME", "").strip()
ADMIN_DASHBOARD_PASSWORD = os.getenv("ADMIN_DASHBOARD_PASSWORD", "")
TIENDANUBE_WEBHOOKS_ENABLED = (
    os.getenv("TIENDANUBE_WEBHOOKS_ENABLED", "false").lower() == "true"
)
# A payment can arrive outside Meta's 24-hour customer-service window.  Never
# send it as free-form text: activate this only after an approved Meta template
# with the supplied name exists.
PAYMENT_CONFIRMED_TEMPLATE_NAME = os.getenv("PAYMENT_CONFIRMED_TEMPLATE_NAME", "").strip()
PAYMENT_CONFIRMED_TEMPLATE_LANGUAGE = os.getenv(
    "PAYMENT_CONFIRMED_TEMPLATE_LANGUAGE", "es_AR"
).strip()
DAILY_SUMMARY_ENABLED = os.getenv("DAILY_SUMMARY_ENABLED", "false").lower() == "true"
DAILY_SUMMARY_TEMPLATE_NAME = os.getenv("DAILY_SUMMARY_TEMPLATE_NAME", "").strip()
DAILY_SUMMARY_TEMPLATE_LANGUAGE = os.getenv("DAILY_SUMMARY_TEMPLATE_LANGUAGE", "es_AR").strip()
# Separate issue from Fred Core: production logs showed Meta rejecting this
# exact template/language pair ("template name (escalacion_isa) does not
# exist in es_AR"), meaning the template was approved under a different
# language code in Meta Business Manager than the one hardcoded here. Fixed
# as a config value, not a guess at the real code -- set
# ESCALACION_ISA_TEMPLATE_LANGUAGE in Railway to whatever Meta actually shows
# as approved for this template (commonly "es" for generic Spanish).
ESCALACION_ISA_TEMPLATE_LANGUAGE = os.getenv("ESCALACION_ISA_TEMPLATE_LANGUAGE", "es_AR").strip()
# Client-facing policy document. The Railway public domain is a safe fallback
# for the current deployment; a custom domain can replace it later without a
# code change.
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL", "https://beauty-house-bot-production-4af8.up.railway.app"
).rstrip("/")
ENCARGOS_PDF_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "policies" / "preventa-encargos-vigente.pdf"
)
ENCARGOS_PDF_URL = "{}/documents/preventa-encargos.pdf".format(PUBLIC_BASE_URL)
CUSTOMER_POLICIES_URL = "https://beautyhousemakeup.com/politicas/"
ARGENTINA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
_reminder_task = None
_message_worker_task = None
_message_worker_id = "{}:{}:{}".format(socket.gethostname(), os.getpid(), secrets.token_hex(4))

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768

# Built on first use, never at import. Importing this module must not require
# any external credential: the test suite imports it to exercise pure routing
# and formatting logic, and CI has no keys by design. Constructing the client
# eagerly made `import app` fail with "Missing key inputs argument!", which
# says nothing about the real problem and happens before any test can run.
#
# The same rule already governs DeepSeek in agent.py (the key is read inside
# the call and a missing one raises a plain message), so this keeps one
# pattern for external credentials instead of two.
_gemini_client = None
_gemini_client_lock = threading.Lock()


def gemini_client():
    """The Gemini client, created on first real use.

    Raises a clear, actionable error when the key is missing -- at the moment
    something actually needs Gemini, not at import. Nothing here catches
    errors coming FROM Gemini: an expired key, a quota error or an outage must
    surface exactly as the provider reported it.
    """
    global _gemini_client
    with _gemini_client_lock:
        if _gemini_client is None:
            # Read here rather than at import so a key configured later in the
            # process lifetime (or in a test) is honoured.
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "Falta GEMINI_API_KEY en las variables de entorno."
                )
            _gemini_client = genai.Client(api_key=api_key)
        return _gemini_client


# ============================================================
# GEMINI EMBEDDINGS
# ============================================================

def embed_text(text: str, task_type: str = "RETRIEVAL_QUERY") -> list:
    """Genera embedding de 768 dimensiones, normalizado."""

    result = gemini_client().models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIMS,
            task_type=task_type,
        ),
    )

    vector = np.array(result.embeddings[0].values)

    normalized = (
        vector / np.linalg.norm(vector)
    ).tolist()

    return normalized


# ============================================================
# BÚSQUEDA RAG EN SUPABASE
# ============================================================

def search_similar_products(query: str, limit: int = 3, query_embedding=None) -> str:
    """
    Busca identidad de producto con recuperación híbrida en Supabase.

    Primero prioriza coincidencias léxicas acotadas (nombre/variante/SKU) y
    luego combina candidatas semánticas que superan un umbral. Nunca devuelve
    stock o precio: esos datos siguen siendo responsabilidad de Tiendanube.
    """

    try:
        embedding = query_embedding or embed_text(query, task_type="RETRIEVAL_QUERY")

        conn = psycopg2.connect(SUPABASE_DB_URL)
        cursor = conn.cursor()

        lexical_rows = []
        lexical_search = lexical_catalog_query(query, limit)
        if lexical_search:
            cursor.execute(*lexical_search)
            lexical_rows = [
                _catalog_row_from_tuple(row) for row in cursor.fetchall()
            ]

        cursor.execute(
            """
            SELECT
                product_id,
                variant_id,
                sku,
                product_name,
                variant,
                1 - (embedding <=> %s::vector) AS similarity
            FROM product_embeddings
            WHERE published = true
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (
                str(embedding),
                str(embedding),
                max(limit * 4, 8),
            ),
        )

        semantic_rows = [
            _catalog_row_from_tuple(row) for row in cursor.fetchall()
        ]

        cursor.close()

        candidates = fuse_catalog_candidates(
            lexical_rows, semantic_rows, limit=limit * 2
        )
        # Wholesale packs are valid only when the customer explicitly asks for
        # wholesale. A normal retail recommendation must not surface them.
        if "mayorista" not in _normalized_text(query):
            candidates = [
                item for item in candidates
                if "mayorista" not in _normalized_text(item.product_name)
            ]
        candidates = candidates[:limit]
        return format_catalog_context(candidates)

    except Exception as error:  # noqa: BLE001

        # Database-driver errors can echo the full connection string. Never
        # write them to logs because it may contain SUPABASE_DB_URL secrets.
        print(
            "ERROR en search_similar_products "
            f"(tipo: {type(error).__name__})"
        )

        return ""


def _catalog_row_from_tuple(row):
    """Adapt the database tuple to the pure catalog retrieval helpers."""
    (
        product_id,
        variant_id,
        sku,
        product_name,
        variant,
        similarity,
    ) = row
    return {
        "product_id": product_id,
        "variant_id": variant_id,
        "sku": sku,
        "product_name": product_name,
        "variant": variant,
        "similarity": similarity,
    }


def _catalog_retrieval_query(
    message_text: str, prior_history: list, active_product_name: str = "",
) -> str:
    """Keep a short product/category reference through natural follow-ups.

    Fred Core's own active_product (when known) is the most reliable of these
    signals -- prepending it never causes a WRONG product to be selected
    (selection still requires the customer's own message to name the
    candidate, see _live_product_candidate), it only keeps retrieval from
    going blind on a bare follow-up like "¿cómo quedan?" that doesn't repeat
    the product name.
    """
    parts = [active_product_name] if active_product_name else []
    normalized = _normalized_text(message_text)
    follow_up_markers = ("chocolate", "color", "esa", "ese", "otra", "otro", "tambien")
    if any(marker in normalized for marker in follow_up_markers):
        previous_customer = next(
            (
                str(item.get("content") or "").strip()
                for item in reversed(prior_history or [])
                if item.get("role") == "user" and str(item.get("content") or "").strip()
            ),
            "",
        )
        if previous_customer:
            parts.append(previous_customer)
    parts.append(message_text)
    return " ".join(part for part in parts if part).strip()


def _fetch_in_parallel(fetch, items, error_label: str) -> list:
    """Run `fetch` over `items` concurrently, returning results in INPUT ORDER
    with None wherever the call raised.

    Purely a scheduling change. Each call is independent (different product,
    different SKU), nobody reads another's result, and the caller still walks
    the outcomes sequentially afterwards -- so the reply Fred builds is
    identical, only sooner. A single item runs inline: a thread would add
    latency, not remove it.

    Errors keep their existing shape: caught per item, logged with the same
    message, and skipped by the caller. One product failing must never take
    down the others, exactly as in the sequential version.
    """
    def guarded(item):
        try:
            return fetch(item)
        except Exception as error:  # noqa: BLE001
            print("ERROR {} (tipo: {}).".format(error_label, type(error).__name__))
            return None

    items = list(items)
    if len(items) <= 1:
        return [guarded(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
        return list(pool.map(guarded, items))


def _count_live_call(live_calls: dict = None) -> None:
    """Record one outbound Tiendanube request. Purely additive bookkeeping:
    it never raises, never blocks, and never influences what gets fetched."""
    if live_calls is None:
        return
    try:
        live_calls["count"] = live_calls.get("count", 0) + 1
    except Exception:  # noqa: BLE001
        pass


def _live_candidate_context(
    catalog_context: str, query: str = "", limit: int = 3, live_calls: dict = None,
) -> str:
    """Verify RAG candidates before the model writes a recommendation.

    live_calls is an optional counter the caller owns (never module state, so
    concurrent turns can't contaminate each other). It only counts; nothing
    here reads it or behaves differently because of it.
    """
    normalized_query = _normalized_text(query)
    requires_lashes = "pestana" in normalized_query
    product_ids = []

    # Color is an attribute, not an identity: querying the exact color in the
    # live store is more reliable than asking vectors to distinguish lashes
    # from every lipstick, brow product or liner that can be "chocolate".
    if requires_lashes and "chocolate" in normalized_query:
        try:
            _count_live_call(live_calls)
            for product in search_available_products("chocolate", limit=10):
                product_id = str(product.get("product_id") or "")
                if product_id and product_id not in product_ids:
                    product_ids.append(product_id)
        except Exception as error:  # noqa: BLE001
            print("ERROR buscando color en Tiendanube (tipo: {}).".format(type(error).__name__))

    for match in re.findall(r"product_id:\s*(\d+)", catalog_context or ""):
        if match not in product_ids:
            product_ids.append(match)
        if len(product_ids) >= limit:
            break

    # Every candidate is an independent lookup, so they are fetched together
    # rather than one after another. Counting happens here, in the calling
    # thread, so the tally never races.
    for _ in product_ids:
        _count_live_call(live_calls)
    availabilities = _fetch_in_parallel(
        lambda product_id: get_product_availability(int(product_id)),
        product_ids,
        "verificando candidata Tiendanube",
    )

    verified = []
    for availability in availabilities:
        if availability is None or not availability.get("found"):
            continue
        in_stock = [
            variant for variant in availability.get("variants", [])
            if variant.get("status") == "in_stock"
        ]
        if not in_stock:
            continue
        variants = ", ".join(
            variant.get("variant") or "variante única" for variant in in_stock[:3]
        )
        # A direct purchase can use this already verified SKU to open the
        # intake form without asking the model to rediscover the same product.
        verified_sku = in_stock[0].get("sku") if len(in_stock) == 1 else ""
        description = re.sub(r"\s+", " ", availability.get("description") or "").strip()
        product_url = str(availability.get("product_url") or "").strip()
        product_text = _normalized_text(
            "{} {}".format(availability.get("product_name") or "", description)
        )
        if requires_lashes and "pestana" not in product_text:
            continue
        lash_type = _lash_type_from_catalog(
            availability.get("product_name") or "", description, variants,
        )
        verified.append(
            "- {} | variantes disponibles: {}{}{}{}{}".format(
                availability.get("product_name") or "Producto",
                variants,
                " | TIPO CONFIRMADO: {}".format(lash_type) if lash_type else "",
                " | SKU: {}".format(verified_sku) if verified_sku else "",
                " | Link: {}".format(product_url) if product_url.startswith("https://") else "",
                " | Descripción: {}".format(description[:420]) if description else "",
            )
        )
    if not verified:
        return ""
    return (
        "Disponibilidad Tiendanube verificada para candidatas recuperadas: "
        "estas opciones tienen stock positivo ahora. No digas que no hay stock "
        "ni las reemplaces por otra categoría. Donde diga TIPO CONFIRMADO, ese "
        "es el tipo real del producto según su ficha: usalo tal cual para "
        "aplicar la guía aprobada (reutilización, lifting, aplicación) y no "
        "deduzcas el tipo por tu cuenta ni por el nombre.\n{}"
    ).format("\n".join(verified))


def _lash_type_from_catalog(product_name: str, description: str, variants: str = "") -> str:
    """Classify a lash product as cluster or banda completa from its own
    catalog record, so the model never has to guess.

    The approved Knowledge gives very different guidance per type
    (reutilización, lifting, aplicación), so getting the type wrong turns a
    correct fact into wrong advice -- exactly what happened in production when
    the model called a cluster product "de banda" and told the customer it was
    reusable. Only an unambiguous signal produces a label: when the record says
    nothing decisive, this returns "" and the model keeps whatever caution it
    would otherwise apply, instead of being handed a confident wrong answer.
    """
    text = _normalized_text(" ".join((product_name, description, variants)))
    cluster_signals = ("cluster", "clusters", "grupos de fibras", "grupo de fibras")
    banda_signals = ("banda completa", "banda fina", "banda intermedia", "pestanas de banda")
    has_cluster = any(signal in text for signal in cluster_signals)
    has_banda = any(signal in text for signal in banda_signals)
    # "10 pares"/"20 pares" presentations are banda completa per approved
    # Knowledge, but that only decides when nothing contradicts it.
    if not has_cluster and not has_banda and re.search(r"\b\d{1,2}\s*(?:pares|pairs)\b", text):
        has_banda = True
    if has_cluster == has_banda:
        return ""
    return "cluster (grupos de fibras)" if has_cluster else "banda completa"


def _grounded_lash_recommendation(live_context: str, query: str) -> str:
    """Give a reliable first recommendation when live catalog facts are exact.

    This deliberately bypasses the language model only for the narrow, common
    case where a customer asks for natural chocolate lashes and Tiendanube has
    already confirmed them. It prevents a generic tool query from contradicting
    the verified options with a false "no stock" reply.
    """
    normalized_query = _normalized_text(query)
    if not (
        "pestana" in normalized_query
        and "chocolate" in normalized_query
        and any(term in normalized_query for term in ("busco", "natural", "todos los dias"))
        and "Disponibilidad Tiendanube verificada" in (live_context or "")
    ):
        return ""

    candidates = re.findall(
        r"^-\s*(.*?)\s*\| variantes disponibles:\s*([^|\n]+)(?:\s*\| SKU:\s*[^|\s]+)?(?:\s*\| Link:\s*(https://[^|\s]+))?",
        live_context,
        flags=re.MULTILINE,
    )
    if not candidates:
        return ""

    options = []
    for product_name, variants, product_url in candidates[:2]:
        friendly_name = re.sub(r"^SHOOW\s+TOOLS\s*-\s*", "", product_name, flags=re.IGNORECASE)
        friendly_name = friendly_name.strip().title().replace("(Chocolate)", "(chocolate)")
        option = "• {} — {}".format(friendly_name, variants.strip())
        if product_url:
            option = "{}\n{}".format(option, product_url)
        options.append(option)

    opener = "¡Sí! Para un look natural de todos los días, tengo estas opciones en chocolate con stock confirmado:"
    return "{}\n\n{}\n\n¿Cuál te gusta más? Si querés, te cuento la diferencia o avanzamos con la compra 😊".format(
        opener,
        "\n".join(options),
    )


def _expresses_purchase(text: str) -> bool:
    """Recognize a purchase decision without mistaking a product question for one."""
    return bool(
        re.search(
            r"\b(comprar|compra|pedir|pido|ordenar|llevar|llevo|avanzar|avancemos|"
            r"proceder|me quedo con|quiero\s+\d+|necesito\s+\d+)\b",
            _normalized_text(text),
        )
    )


def _live_product_candidate(live_context: str, message_text: str) -> dict:
    """Resolve one unambiguous, already-live-verified product mention.

    Selection and purchase are deliberately different states: a client can ask
    about Isabel I now and write “quiero dos” in the next WhatsApp message.
    The selection is persisted, while a sale is opened only after purchase
    intent is explicit.
    """
    normalized_message = _normalized_text(message_text)
    candidates = []
    pattern = (
        r"^-\s*(.*?)\s*\| variantes disponibles:\s*([^|\n]+)"
        r"(?:\s*\| SKU:\s*([^|\s]+))?"
    )
    for product_name, variant, sku in re.findall(pattern, live_context or "", flags=re.MULTILINE):
        short_name = re.sub(r"^SHOOW\s+TOOLS\s*-\s*", "", product_name, flags=re.IGNORECASE)
        distinctive_words = [
            word for word in re.findall(r"[a-z0-9]+", _normalized_text(short_name))
            if len(word) >= 3 and word not in {"chocolate", "pestanas", "pestana"}
        ]
        if distinctive_words and all(word in normalized_message for word in distinctive_words):
            candidates.append((product_name, variant.strip(), sku.strip()))

    # “Chocolate” alone can name either candidate. Only an actual model name
    # opens a purchase form; otherwise Fred keeps the natural choice question.
    if len(candidates) != 1:
        return {}

    product_name, variant, sku = candidates[0]
    if not sku:
        # More than one live variant for this exact product (e.g. two lash
        # lengths), so _live_candidate_context couldn't verify a single SKU.
        # The PRODUCT is still unambiguous -- identify it so Fred Core can
        # anchor to it and ask specifically for the variant later, instead of
        # silently guessing one or failing to recognize the product at all.
        return {"sku": "", "product_name": product_name, "variant": "", "unit_price": None}
    try:
        stock = get_stock(sku)
    except Exception as error:  # noqa: BLE001
        print("ERROR revalidando compra directa (tipo: {}).".format(type(error).__name__))
        return {}
    if stock.get("status") != "in_stock":
        return {}
    return {
        "sku": stock.get("sku") or sku,
        "product_name": stock.get("product_name") or product_name,
        "variant": stock.get("variant") or variant,
        "unit_price": stock.get("price"),
    }


def _live_purchase_candidate(live_context: str, message_text: str) -> dict:
    """Return a verified candidate only when the same message asks to buy."""
    if not _expresses_purchase(message_text):
        return {}
    return _live_product_candidate(live_context, message_text)


_ISA_INSTRUCTION_PREFIX_RE = re.compile(
    r"^\s*(?:ok(?:ey|ay)?|dale|listo|perfecto|bueno)?[\s,.:;-]*"
    r"(?:por\s+favor\s+)?"
    # Accents are written naturally ("envíale", "decile", "avisá"), so every
    # vowel that can carry one is matched with or without it.
    r"(?:env[ií]\w*|mand[aá]\w*|dec[ií]\w*|pregunt[aá]\w*|coment[aá]\w*|"
    r"avis[aá]\w*|explic[aá]\w*|contest[aá]\w*|respond[eé]\w*)"
    r"(?:\s*(?:le|les|selo|se\s+lo|a\s+la\s+clienta|al\s+cliente|a\s+ella|"
    r"a\s+la\s+chica|a\s+la\s+se[ñn]ora))*"
    r"\s*(?:que|:)?\s*",
    flags=re.IGNORECASE,
)


def _draft_quantity(sale_draft: dict) -> int:
    """Quantity as recorded on the card ("3 × PRODUCTO")."""
    try:
        return int(str((sale_draft or {}).get("items_status", "")).split("×", 1)[0].strip())
    except (ValueError, IndexError):
        return 0


def _product_url_for_sku(sku: str) -> str:
    """The real public Tiendanube page for this SKU. The sale happens there:
    Fred never rebuilds cart, address or payment inside WhatsApp."""
    if not sku:
        return ""
    try:
        for product in search_available_products(sku, limit=5):
            for variant in product.get("variants") or []:
                if str(variant.get("sku") or "").strip().lower() == sku.strip().lower():
                    availability = get_product_availability(int(product["product_id"]))
                    url = str(availability.get("product_url") or "").strip()
                    return url if url.startswith("https://") else ""
    except Exception as error:  # noqa: BLE001
        print("ERROR obteniendo link del producto (tipo: {}).".format(type(error).__name__))
    return ""


def _classify_checkout_failure(sale_draft: dict, error: Exception) -> str:
    """Tell Isa what actually went wrong, checked against the live store.

    "No hay stock" and "este borrador apunta a un producto que no existe" need
    completely different actions from her, and conflating them (as production
    did) sends her to fix a commercial problem that isn't there.
    """
    sku = str((sale_draft or {}).get("selected_sku") or "").strip()
    detail = str(error).strip()
    if not sku or sku.lower() in ("a confirmar", "none"):
        return (
            "⚠️ Error de integridad: este pedido no tiene un SKU real guardado, "
            "así que no puedo identificar qué variante vender. No es un problema "
            "de stock. Conviene rehacer la compra con la clienta."
        )
    try:
        stock = get_stock(sku)
    except Exception:  # noqa: BLE001
        return "No pude verificar el producto en vivo ahora mismo. Detalle: {}".format(detail)
    if not stock.get("found"):
        return (
            "⚠️ Error de integridad: el SKU {} no existe en la tienda. No es falta "
            "de stock: el pedido quedó apuntando a un producto equivocado."
        ).format(sku)
    if stock.get("status") != "in_stock":
        return "El producto {} no está disponible para la venta ahora.".format(
            stock.get("product_name") or sku,
        )
    available = stock.get("quantity")
    if isinstance(available, int):
        return (
            "El producto existe y figura con {} unidades, así que no es falta de "
            "stock. Tiendanube rechazó la creación del link. Detalle: {}"
        ).format(available, detail)
    return "No se pudo crear el link. Detalle: {}".format(detail)


def _reason_for_customer(text: str) -> str:
    """Strip Isa's instruction-to-Fred wrapper so the customer reads the fact,
    not the order that produced it.

    Isa writes "ok envíale al cliente que no hay stock"; the customer must
    read "no hay stock", never the instruction itself. Only the leading
    wrapper is removed -- the substance is always preserved verbatim.
    """
    cleaned = " ".join((text or "").split())
    stripped = _ISA_INSTRUCTION_PREFIX_RE.sub("", cleaned, count=1).strip()
    if not stripped:
        return cleaned
    return stripped[0].upper() + stripped[1:] if stripped else cleaned


BUY_BUTTON_PREFIX = "buy:"
ISA_BUTTON_ID = "start_isa_consultation"


def send_customer_action_buttons(phone_number: str, text: str, buttons: list) -> bool:
    """Offer the customer the explicit actions they can take.

    These buttons are the ONLY way into checkout or an Isa consultation: no
    phrase Fred reads can start either one. Each buy button carries the real
    SKU, so the click itself fixes the product identity and the checkout never
    has to guess what was meant.
    """
    if _real_outbound_is_blocked("botones de acción"):
        return False
    delivery_context = current_delivery_context.get()
    if delivery_context and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(delivery_context.customer_phone)
    ):
        delivery_context.attempted = True
        if not processing_claim_is_current(
            delivery_context.conversation_id,
            delivery_context.generation,
            delivery_context.worker_id,
        ):
            delivery_context.stale_discarded = True
            print("[Conversacion] Botones obsoletos descartados por generation/lease.")
            return False
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(phone_number),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text[:1024]},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": button["id"], "title": button["title"][:20]}}
                for button in buttons[:3]
            ]},
        },
    }
    try:
        response = requests.post(
            f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        if delivery_context and (
            normalize_whatsapp_recipient(phone_number)
            == normalize_whatsapp_recipient(delivery_context.customer_phone)
        ):
            delivery_context.delivered = True
        return True
    except Exception as error:  # noqa: BLE001
        print("ERROR enviando botones de acción: {}".format(type(error).__name__))
        return False


# --- turn observability -----------------------------------------------
#
# Two lines per agent turn, greppable and machine-parseable, so "Fred is slow"
# and "Fred answered wrong" stop being anecdotes. Purely descriptive: nothing
# here decides anything, and every helper swallows its own errors -- an
# observability bug must never cost a customer their answer.
#
# Phases are wall-clock and sequential, matching how the turn actually runs
# today. They do not sum to total_ms: total_ms also covers store reads,
# formatting and delivery, and the gap between the two is itself a signal.

_TIMING_PHASES = ("knowledge_ms", "catalog_ms", "live_stock_ms", "llm_ms")


def _new_turn_timings() -> dict:
    return {phase: 0.0 for phase in _TIMING_PHASES}


@contextmanager
def _timed(timings: dict, phase: str):
    """Accumulate elapsed ms into one phase. Accumulates rather than assigns:
    catalog and live verification are each entered more than once per turn,
    and what matters is the total spent there, not the last visit."""
    started = time.monotonic()
    try:
        yield
    finally:
        try:
            timings[phase] = timings.get(phase, 0.0) + (time.monotonic() - started) * 1000
        except Exception:  # noqa: BLE001 - never let measurement break a turn
            pass


def _log_turn_timing(
    timings: dict,
    *,
    started_at: float,
    tool_calls: int = 0,
    tokens_input: int = 0,
    tokens_output: int = 0,
) -> None:
    try:
        fields = " ".join([
            "total_ms={}".format(round((time.monotonic() - started_at) * 1000)),
            " ".join(
                "{}={}".format(phase, round((timings or {}).get(phase, 0.0)))
                for phase in _TIMING_PHASES
            ),
            "tool_calls={}".format(tool_calls),
            "tokens_input={}".format(tokens_input),
            "tokens_output={}".format(tokens_output),
        ])
        print("[FredTiming] {}".format(fields))
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando timing del turno (tipo: {}).".format(type(error).__name__))


# The words that identify a product, versioned with the code (see
# api/build_product_lexicon.py). This used to be derived from
# data/catalog.json, which .gitignore excludes as a locally generated dump --
# so it existed on one machine and nowhere else, and CI and production both
# built an empty lexicon. An empty lexicon does not fail safe: it silently
# switches OFF the "customer named a product" blocker, which is the guard that
# keeps a product question from being answered out of a policy document.
_PRODUCT_LEXICON_PATH = Path(__file__).resolve().parents[1] / "data" / "product_lexicon.txt"
_product_lexicon_cache = None
_product_lexicon_status = "unloaded"


def _load_product_lexicon() -> None:
    """Read the versioned lexicon once, and say plainly what happened."""
    global _product_lexicon_cache, _product_lexicon_status
    words = set()
    try:
        for line in _PRODUCT_LEXICON_PATH.read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                words.add(word)
        _product_lexicon_status = "ok" if words else "empty"
    except FileNotFoundError:
        _product_lexicon_status = "missing"
    except Exception as error:  # noqa: BLE001
        _product_lexicon_status = "error:{}".format(type(error).__name__)

    _product_lexicon_cache = frozenset(words)
    print("[FredCatalog] product_lexicon={} status={} source={}".format(
        len(_product_lexicon_cache), _product_lexicon_status,
        _PRODUCT_LEXICON_PATH.name,
    ))
    if _product_lexicon_status != "ok":
        # Loud on purpose. Without this list Fred cannot tell that a customer
        # named a product, and the turn would look answerable from Knowledge.
        print(
            "ERROR CRÍTICO: sin léxico de productos, Fred no puede reconocer "
            "que una clienta nombró un producto. Regenerá con "
            "`python api/build_product_lexicon.py`. Mientras tanto ningún "
            "turno se clasifica como knowledge_only."
        )


def product_lexicon() -> frozenset:
    """Identifying words from the real catalog, loaded once per process.

    Never a live call: this recognises that a customer NAMED something, and
    never claims the thing exists or is sellable -- those still require the
    store, per request. Product families change far more slowly than stock.
    """
    if _product_lexicon_cache is None:
        _load_product_lexicon()
    return _product_lexicon_cache


def product_lexicon_available() -> bool:
    """Whether the product-name blocker can actually do its job.

    An empty lexicon counts as unavailable, not as "no products matched":
    those two look identical to a caller and only one of them is safe.
    """
    return bool(product_lexicon()) and _product_lexicon_status == "ok"


def _log_turn_knowledge(
    *, embedding_status: str, retrieval_hits: int = 0, embedding_error: str = "",
) -> None:
    """Whether Knowledge actually ran, and why not when it didn't.

    This exists because an embedding failure silently disables the ENTIRE
    Knowledge block (app skips it when query_embedding is None), so Fred loses
    every approved answer and degrades to catalog-only with nothing in the logs
    to say so. Under a provider rate limit that looks identical to "the KB has
    no chunk for this" -- a content problem and an infrastructure problem
    wearing the same face. This line tells them apart.
    """
    try:
        print("[FredKnowledge] embedding_status={} retrieval_hits={} embedding_error={}".format(
            embedding_status or "unknown",
            retrieval_hits,
            embedding_error or "none",
        ))
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando knowledge del turno (tipo: {}).".format(type(error).__name__))


def _log_turn_routing(routing_requirement: dict = None, *, live_calls: dict = None) -> None:
    """What this turn needed, versus what it actually spent.

    data_required is the policy's verdict (routing_policy.classify_turn_data_
    requirement). skipped_live is not hypothetical: it reports whether this
    turn genuinely made zero Tiendanube requests, counted at the call sites.

    The pair is the measurement. A turn logged as
        data_required=knowledge_only skipped_live=false
    is a request Fred paid for and did not need -- counting those across real
    traffic is what says whether the cut is worth making, and how much.
    """
    try:
        requirement = routing_requirement or {}
        calls = (live_calls or {}).get("count", 0)
        print("[FredRouting] intent={} data_required={} skipped_live={} reason={}".format(
            requirement.get("intent") or "unknown",
            requirement.get("data_required") or "unknown",
            "true" if not calls else "false",
            requirement.get("reason") or "unknown",
        ))
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando routing del turno (tipo: {}).".format(type(error).__name__))


def _log_turn_decision(
    *,
    topic=None,
    grounded_by: str = "",
    core_state: dict = None,
    buttons_added: bool = False,
) -> None:
    """What Fred concluded, in the same shape every turn. active_product and
    active_sku are read from Fred Core at the END of the turn, so the line
    reflects what the NEXT message will actually be anchored to -- which is
    the thing worth auditing after a wrong answer."""
    try:
        state = core_state or {}
        print("[FredDecision] topic={} grounded_by={} active_product={} active_sku={} buttons_added={}".format(
            topic or "none",
            grounded_by or "none",
            state.get("active_product_name") or "none",
            state.get("active_sku") or "none",
            "yes" if buttons_added else "no",
        ))
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando decisión del turno (tipo: {}).".format(type(error).__name__))


def _offer_customer_actions(
    conversation_id: int, customer_phone: str, reply_text: str, core_state: dict,
    offer_isa: bool = False,
) -> bool:
    """No action buttons: Fred does not sell.

    Fred's scope no longer includes closing a sale, so the [Comprar] button --
    the only entry point into CHECKOUT -- is not offered at all. Purchase
    intent goes to Isa with the product, variant and quantity the customer
    already gave, instead of opening a checkout.

    This is enforced by the contract, not by configuration: it returns False
    whatever SALES_INTAKE_ENABLED or TIENDANUBE_CHECKOUT_MODE happen to be, so
    an env var cannot accidentally put Fred back in the selling business.

    Kept as a function rather than deleted so the call site, its tests and the
    checkout code behind it stay intact while the new scope is validated in
    production. Reconnecting it is one return statement.
    """
    return False


# Personalised advice: the customer is asking what suits THEM, which Isa
# prefers to take herself. Fred still gives grounded orientation first; the
# button is only offered alongside it.
_ADVICE_REQUEST_RE = re.compile(
    r"\b(me\s+recomend\w+|que\s+me\s+recomend\w+|cual\s+me\s+conviene|"
    r"cual\s+me\s+queda|que\s+me\s+queda|me\s+recomendaron|asesor\w+|"
    r"cual\s+eleg\w+|no\s+se\s+cual\s+eleg\w+|que\s+me\s+sugeris)\b"
)


def _is_personalised_advice(message_text: str) -> bool:
    return bool(_ADVICE_REQUEST_RE.search(_normalized_text(message_text)))


def _start_purchase_from_button(
    conversation_id: int, customer_phone: str, sku: str, core_state: dict,
) -> str:
    """The click already decided the product. Verify that exact SKU live and
    open the draft on it -- never re-discover what the customer meant."""
    try:
        stock = get_stock(sku)
    except Exception as error:  # noqa: BLE001
        print("ERROR verificando SKU del botón (tipo: {}).".format(type(error).__name__))
        return "No pude confirmar ese producto en este momento. ¿Probamos de nuevo en un minuto?"
    if not stock.get("found"):
        return "Ese producto ya no está disponible. ¿Querés que busquemos otra opción?"
    if stock.get("status") != "in_stock":
        return "Justo {} se quedó sin stock. ¿Buscamos otra opción?".format(
            stock.get("product_name") or "ese producto",
        )
    candidate = {
        "sku": stock.get("sku") or sku,
        "product_name": stock.get("product_name"),
        "variant": stock.get("variant") or "",
        "unit_price": stock.get("price"),
    }
    save_fred_core_state(
        conversation_id, mode="CHECKOUT", quantity=core_state.get("quantity"),
        **_fred_core_active_product_fields(candidate),
    )
    return _start_sales_intake(
        conversation_id, candidate, quantity=core_state.get("quantity") or 0,
    )


def _purchase_draft_integrity_error(intake: dict) -> str:
    """Why this draft must NOT become a summary or an Isa review, or "".

    The invariant: a purchase may only be presented or escalated when its
    product identity is real and still sellable. Checking it here means a
    contaminated draft can never reach the customer as a confident summary,
    nor reach Isa as a card she could approve.
    """
    sku = str((intake or {}).get("selected_sku") or "").strip()
    if not sku or sku.lower() in ("a confirmar", "none"):
        return "sin SKU real"
    try:
        stock = get_stock(sku)
    except Exception as error:  # noqa: BLE001
        print("ERROR verificando integridad del borrador (tipo: {}).".format(type(error).__name__))
        return "no pude verificar el producto en vivo"
    if not stock.get("found"):
        return "el SKU {} no existe en la tienda".format(sku)
    if stock.get("status") != "in_stock":
        return "el producto no está disponible para la venta"
    quantity = intake.get("quantity") or 0
    available = stock.get("quantity")
    if isinstance(available, int) and quantity and available < quantity:
        return "stock insuficiente: quedan {} y pediste {}".format(available, quantity)
    return ""


# "SHOOW TOOLS - ISABEL I (CHOCOLATE)" is a store name; "Isabel I (Chocolate)"
# is what a person says. Stripping the brand is presentation only -- identity
# always stays the full catalog name plus the SKU.
_BRAND_PREFIX_RE = re.compile(r"^(?:PRE\s*VENTA\s*-\s*)?SHOOW\s*TOOLS\s*-\s*", re.IGNORECASE)
# A preorder has no immediate stock and its price/date are quoted case by case
# (legacy_special_sale_context already routes "preventa" to Isa). It must never
# appear among the options for a normal, immediate purchase.
_PREORDER_NAME_RE = re.compile(r"\bPRE\s*VENTA\b", re.IGNORECASE)
_MAX_VARIANT_OPTIONS = 5
# Words that describe a category or a house brand rather than identify a
# product. "beauty" matches ten unrelated things in this catalog, so it can
# never be the word that establishes what the customer meant.
_NON_IDENTIFYING_WORDS = frozenset({
    "pestanas", "pestana", "shoow", "tools", "beauty", "house", "cosmetics",
    "producto", "productos", "version", "pack", "set", "kit", "color", "colores",
    "maquillaje", "perfume", "perfumes",
})


def _identifying_words(text: str) -> list:
    """The words in `text` that could actually pin down a product."""
    return [
        word for word in re.findall(r"[a-z0-9]+", _normalized_text(text))
        if len(word) >= 3 and word not in _NON_IDENTIFYING_WORDS
    ]


def _message_names(message_text: str, label: str) -> bool:
    """Did the customer actually say this, in their own message?

    Every identifying word of `label` has to be present. This is the strict
    half of the commercial rule ("producto mencionado explícitamente por la
    clienta") and it is what separates "quiero comprar Isabel I" -- where
    "isabel" really is in the message -- from "un perfume de Rare Beauty",
    where the live search returns products that merely share a common word.
    A substring hit in the store is a coincidence; the customer's own words
    are the evidence.
    """
    normalized_message = _normalized_text(message_text)
    words = _identifying_words(label)
    return bool(words) and all(word in normalized_message for word in words)


def _short_product_label(product_name: str) -> str:
    return _BRAND_PREFIX_RE.sub("", str(product_name or "")).strip() or str(product_name or "").strip()


def _shared_product_label(product_names: list) -> str:
    """The leading words every matched name has in common -- "Isabel I" for the
    fourteen "SHOOW TOOLS - ISABEL I (...)" products. Taken from real catalog
    names, never from the customer's phrasing and never invented: when the
    names share no common start, this returns "" and the caller asks without
    naming anything."""
    word_lists = [_short_product_label(name).split() for name in product_names if name]
    if not word_lists:
        return ""
    shared = []
    for position in range(min(len(words) for words in word_lists)):
        candidates = {words[position].lower() for words in word_lists}
        if len(candidates) != 1:
            break
        shared.append(word_lists[0][position])
    # A single shared word like "(CHOCOLATE)" or a bare "-" identifies nothing.
    label = " ".join(shared).strip(" -–—")
    return label if len(label) >= 3 else ""


def _live_products_named_in_message(
    live_context: str, message_text: str, active_product_name: str,
    live_calls: dict = None,
) -> list:
    """Full live product records (name + in-stock variants) the customer's own
    words point at, excluding the active one and any preorder.

    Probing is deliberately literal: the customer's own distinctive words are
    searched against the live store, never against the semantic index. The
    RAG catalog regularly misses a plainly-named model and, worse, happily
    returns something merely similar -- which is the one thing a purchase may
    never be built on. This keeps the real SKUs and variants rather than
    collapsing them to names, so an ambiguous purchase can be ASKED about
    using the store's actual options.

    Excluding the active product is the guard against the worst failure mode:
    someone says "quiero 4 Taylor" while Isabel I is active, Taylor resolves
    to several products, and the checkout silently opens on Isabel I instead.
    """
    normalized_message = _normalized_text(message_text)
    normalized_active = _normalized_text(active_product_name or "")
    stopwords = {
        "quiero", "comprar", "compro", "llevar", "llevo", "quisiera", "necesito",
        "unidades", "unidad", "pares", "pack", "packs", "cantidad",
        "hola", "buenas", "fred", "porfa", "favor", "gracias", "please", "entonces",
        "tambien", "mejor", "entonce", "bueno", "genial", "entoces",
        "pestanas", "pestana", "shoow", "tools", "version", "producto", "productos",
    }
    probes = [
        word for word in re.findall(r"[a-z0-9]{4,}", normalized_message)
        if word not in stopwords
        and word not in _NON_IDENTIFYING_WORDS
        and word not in normalized_active
    ]
    probes = probes[:4]
    # One search per probe word, and no probe depends on another's result --
    # so they go out together. Results are consumed below in probe order, so
    # `found` ends up in exactly the order the sequential version produced.
    for _ in probes:
        _count_live_call(live_calls)
    searches = _fetch_in_parallel(
        lambda probe: search_available_products(probe, limit=10),
        probes,
        "buscando producto nombrado",
    )

    found = []
    seen_names = set()
    for probe, results in zip(probes, searches):
        if results is None:
            continue
        probe_word = re.compile(r"\b{}\b".format(re.escape(probe)))
        for product in results:
            product_name = str(product.get("name") or "").strip()
            normalized_name = _normalized_text(product_name)
            if not product_name or normalized_name == normalized_active:
                continue
            if _PREORDER_NAME_RE.search(product_name):
                continue
            # Whole word, not substring: Tiendanube's own text search is
            # generous ("rare" pulls in anything containing those letters),
            # and a partial hit is not the customer naming a product.
            if probe_word.search(normalized_name) and product_name not in seen_names:
                seen_names.add(product_name)
                found.append(product)
    return found


def _purchase_identity_from_message(
    live_context: str, message_text: str, active_product_name: str,
    live_calls: dict = None,
) -> dict:
    """Resolve WHAT the customer just asked to buy, under one rule: a SKU is
    never chosen by similarity.

    Identity requires all three -- the customer named it, it matches a real
    catalog product, and live stock confirms it. The three possible outcomes
    map exactly to the commercial rule:

      {"status": "resolved", "candidate": {...}}
          One unambiguous, live, in-stock SKU. Safe to pin and to put behind
          a [Comprar] button.
      {"status": "ambiguous", "label": ..., "options": [...]}
          The words are real and match the catalog, but they name more than
          one sellable thing (fourteen products called "Isabel I", or one
          product with several variants). Fred asks; Fred never picks.
      {"status": "unknown"}
          Nothing the customer said resolves to a live product. The normal
          conversational path continues -- this function stays silent rather
          than guessing.
    """
    products = _live_products_named_in_message(
        live_context, message_text, active_product_name, live_calls,
    )
    if not products:
        return {"status": "unknown"}

    # Every sellable option the named words point at, flattened across
    # products. A product with three in-stock variants is three options, and
    # three products with one variant each is also three -- both are "more
    # than one thing the customer could have meant".
    options = []
    for product in products:
        product_name = str(product.get("name") or "").strip()
        variants = [v for v in (product.get("variants") or []) if str(v.get("sku") or "").strip()]
        for variant in variants:
            description = str(variant.get("description") or "").strip()
            options.append({
                "sku": str(variant["sku"]).strip(),
                "product_name": product_name,
                "variant": description,
                "label": "{}{}".format(
                    _short_product_label(product_name),
                    " — {}".format(description) if description else "",
                ),
            })

    if not options:
        return {"status": "unknown"}

    if len(options) == 1:
        # One candidate is still not enough on its own. Two more gates before
        # it can become purchasable identity:
        #   1. the customer must actually have named it (a lone live match is
        #      not evidence -- the store's search is generous), and
        #   2. that exact SKU must be re-verified live, the same way the
        #      [Comprar] button does. A search result is evidence; get_stock
        #      is confirmation.
        only = options[0]
        if not _message_names(message_text, _short_product_label(only["product_name"])):
            return {"status": "unknown"}
        try:
            _count_live_call(live_calls)
            stock = get_stock(only["sku"])
        except Exception as error:  # noqa: BLE001
            print("ERROR verificando SKU único nombrado (tipo: {}).".format(type(error).__name__))
            return {"status": "unknown"}
        if not stock.get("found") or stock.get("status") != "in_stock":
            return {"status": "unknown"}
        return {
            "status": "resolved",
            "candidate": {
                "sku": stock.get("sku") or only["sku"],
                "product_name": stock.get("product_name") or only["product_name"],
                "variant": stock.get("variant") or only["variant"],
                "unit_price": stock.get("price"),
            },
        }

    # Several sellable things matched. That is only real ambiguity if they are
    # variants of ONE product the customer named -- the several "Isabel I"
    # products -- and not simply everything sharing a brand.
    #
    # Two gates, and both are about evidence rather than judgment:
    #   1. the customer said the shared name themselves, and
    #   2. the store really has a product BY that name.
    # Gate 2 is what separates "Isabel I" (there is a product called exactly
    # that, the rest are its variants) from "Rare Beauty" (a brand: every
    # match is "Rare Beauty - <something else>", and someone asking for a
    # Rare Beauty perfume has not named a product at all). Failing either gate
    # is not an error -- it just means this turn is a conversation, not an
    # identification, so the normal path answers it.
    labels = [option["product_name"] for option in options]
    label = _shared_product_label(labels)
    if not label or not _message_names(message_text, label):
        return {"status": "unknown"}
    if not any(_short_product_label(name).lower() == label.lower() for name in labels):
        return {"status": "unknown"}
    return {"status": "ambiguous", "label": label, "options": options}


def _render_variant_question(identity: dict) -> str:
    """Ask which one, using the store's real options. Never picks, never
    prices something that wasn't verified, and never pretends the list is
    complete when it was capped."""
    options = identity.get("options") or []
    label = identity.get("label") or ""
    opening = (
        "¡Genial! 😊 Tenemos {} en varias opciones:".format(label) if label
        else "¡Genial! 😊 Encontré varias opciones que pueden ser la que buscás:"
    )
    lines = ["• {}".format(option["label"]) for option in options[:_MAX_VARIANT_OPTIONS]]
    closing = (
        "¿Cuál buscabas?"
        if len(options) <= _MAX_VARIANT_OPTIONS
        else "Y algunas más. ¿Cuál buscabas? Si tenés el nombre exacto o el link, mejor todavía."
    )
    return "\n".join([opening] + lines + ["", closing])


def _revalidate_product_candidate(candidate: dict) -> dict:
    """Never use a remembered product without checking current Tiendanube stock."""
    if not candidate or not candidate.get("sku"):
        return {}
    try:
        stock = get_stock(candidate["sku"])
    except Exception as error:  # noqa: BLE001
        print("ERROR revalidando selección (tipo: {}).".format(type(error).__name__))
        return {}
    if stock.get("status") != "in_stock":
        return {}
    return {
        "sku": stock.get("sku") or candidate["sku"],
        "product_name": stock.get("product_name") or candidate.get("product_name"),
        "variant": stock.get("variant") or candidate.get("variant") or "",
        "unit_price": stock.get("price"),
    }


def _search_local_knowledge_bundle(
    query: str,
    limit: int = DEFAULT_KNOWLEDGE_TOP_K,
) -> KnowledgeRetrieval:
    """Read the reviewed Markdown corpus without any external dependency."""
    try:
        return retrieve_local_knowledge(
            query,
            load_knowledge_chunks(KNOWLEDGE_DIRECTORY),
            limit=limit,
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR en knowledge local (tipo: {})".format(type(error).__name__))
        return KnowledgeRetrieval()


def _search_supabase_knowledge_bundle(
    query: str,
    limit: int = DEFAULT_KNOWLEDGE_TOP_K,
    query_embedding=None,
) -> Optional[KnowledgeRetrieval]:
    """Retrieve reviewed Knowledge V1 rows, returning None on provider failure."""
    conn = None
    cursor = None
    try:
        embedding = query_embedding or embed_text(query, task_type="RETRIEVAL_QUERY")
        conn = psycopg2.connect(SUPABASE_DB_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT source_id, section, content, status, active,
                   1 - (embedding <=> %s::vector) AS similarity,
                   COALESCE(to_jsonb(knowledge_chunks) -> 'metadata', '{}'::jsonb) AS metadata
            FROM knowledge_chunks
            WHERE active = true AND status = 'approved'
              AND COALESCE(to_jsonb(knowledge_chunks) -> 'metadata' ->> 'approved_by', '') = 'Isa'
              AND COALESCE(to_jsonb(knowledge_chunks) -> 'metadata' ->> 'id', '') = source_id
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (str(embedding), str(embedding), limit * 3),
        )
        rows = [
            {
                "source_id": row[0], "section": row[1], "content": row[2],
                "status": row[3], "active": row[4], "similarity": row[5],
                "metadata": row[6] or {},
            }
            for row in cursor.fetchall()
        ]
        cursor.close()
        cursor = None
        accepted_rows = approved_knowledge_rows(rows, limit=limit)
        topics = sorted({
            str((row.get("metadata") or {}).get("topic") or "")
            for row in accepted_rows if (row.get("metadata") or {}).get("topic")
        })
        obligation_rows = accepted_rows
        if topics:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT DISTINCT COALESCE(to_jsonb(knowledge_chunks) -> 'metadata', '{}'::jsonb)
                FROM knowledge_chunks
                WHERE active = true AND status = 'approved'
                  AND COALESCE(to_jsonb(knowledge_chunks) -> 'metadata' ->> 'approved_by', '') = 'Isa'
                  AND COALESCE(to_jsonb(knowledge_chunks) -> 'metadata' ->> 'id', '') = source_id
                  AND COALESCE(to_jsonb(knowledge_chunks) -> 'metadata' ->> 'topic', '') = ANY(%s)
                """,
                (topics,),
            )
            obligation_rows = [{"metadata": row[0] or {}} for row in cursor.fetchall()]
            cursor.close()
            cursor = None
        return build_knowledge_retrieval(
            accepted_rows, query=query, obligation_rows=obligation_rows
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR en knowledge Supabase (tipo: {}); fallback local.".format(type(error).__name__))
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def search_knowledge_bundle(
    query: str,
    limit: int = DEFAULT_KNOWLEDGE_TOP_K,
    query_embedding=None,
) -> KnowledgeRetrieval:
    """Select Knowledge source explicitly; Supabase failures fall back to local."""
    if not KNOWLEDGE_RAG_ENABLED:
        return KnowledgeRetrieval()
    if KNOWLEDGE_RAG_SOURCE == "supabase":
        retrieval = _search_supabase_knowledge_bundle(
            query,
            limit=limit,
            query_embedding=query_embedding,
        )
        if retrieval is not None:
            return retrieval
    return _search_local_knowledge_bundle(query, limit=limit)


def search_knowledge_context(
    query: str,
    limit: int = DEFAULT_KNOWLEDGE_TOP_K,
    query_embedding=None,
) -> str:
    """Backward-compatible text-only wrapper used by older callers/tests."""
    return search_knowledge_bundle(query, limit, query_embedding).context


# ============================================================
# WHATSAPP — ENVÍO DE MENSAJES
# ============================================================

def normalize_whatsapp_recipient(phone_number: str) -> str:
    """Usa el formato que Meta registra para números móviles argentinos."""

    # El webhook identifica móviles argentinos como 549..., mientras que
    # la lista de destinatarios de prueba de Meta los registra como 54....
    if phone_number.startswith("549"):
        return f"54{phone_number[3:]}"

    return phone_number


def send_escalacion_isa_template(
    phone_number: str,
    pending_inquiries: int = 1,
) -> bool:
    """Envía la plantilla Meta escalacion_isa con la cantidad de consultas."""
    if _real_outbound_is_blocked("plantilla escalacion_isa"):
        return False

    url = (
        f"https://graph.facebook.com/v26.0/"
        f"{WHATSAPP_PHONE_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    recipient_phone = normalize_whatsapp_recipient(phone_number)

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_phone,
        "type": "template",
        "template": {
            "name": "escalacion_isa",
            "language": {
                "code": ESCALACION_ISA_TEMPLATE_LANGUAGE
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(pending_inquiries),
                        }
                    ],
                }
            ],
        },
    }

    try:

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=10,
        )

        print(
            f"[WhatsApp] HTTP {response.status_code}"
        )

        print(
            f"[WhatsApp] Response: {response.text}"
        )

        response.raise_for_status()

        return True

    except Exception as e:

        print(f"ERROR enviando plantilla a WhatsApp: {e}")

        return False


def _real_outbound_is_blocked(channel: str) -> bool:
    """Refuse to touch the real WhatsApp API from a test run.

    A unit test once messaged Isa for real because a new code path called an
    unmocked sender. Mocking is still the right practice, but forgetting to
    mock must not be able to reach a customer: the block is on by default
    under any test runner and can only be lifted deliberately with
    FRED_ALLOW_REAL_OUTBOUND=true for an explicit live/integration run.
    """
    if os.getenv("FRED_ALLOW_REAL_OUTBOUND", "").strip().lower() == "true":
        return False
    under_test = (
        "PYTEST_CURRENT_TEST" in os.environ
        or "pytest" in sys.modules
        or "unittest" in sys.modules
    )
    if under_test:
        print("[Test] Envío real bloqueado ({}). Mockealo o usá FRED_ALLOW_REAL_OUTBOUND=true.".format(channel))
    return under_test


def send_whatsapp_text(phone_number: str, text: str) -> bool:
    """Send a text reply inside the customer-initiated 24-hour window."""
    delivery_context = current_delivery_context.get()
    if delivery_context and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(delivery_context.customer_phone)
    ):
        delivery_context.attempted = True
        if not processing_claim_is_current(
            delivery_context.conversation_id,
            delivery_context.generation,
            delivery_context.worker_id,
        ):
            delivery_context.stale_discarded = True
            print("[Conversacion] Envío obsoleto descartado por generation/lease.")
            return False
    # Checked after the lease bookkeeping so a blocked test run still exercises
    # the same staleness decisions the real path makes.
    if _real_outbound_is_blocked("texto"):
        return False
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(phone_number),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[WhatsApp] HTTP {response.status_code}")
        print(f"[WhatsApp] Response: {response.text}")
        response.raise_for_status()
        if delivery_context and (
            normalize_whatsapp_recipient(phone_number)
            == normalize_whatsapp_recipient(delivery_context.customer_phone)
        ):
            delivery_context.delivered = True
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando texto a WhatsApp: {type(error).__name__}")
        return False


def send_customer_fulfillment_buttons(phone_number: str) -> bool:
    """Ask the one closed checkout question with native WhatsApp buttons."""
    if _real_outbound_is_blocked("botones de entrega"):
        return False
    delivery_context = current_delivery_context.get()
    if delivery_context and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(delivery_context.customer_phone)
    ):
        delivery_context.attempted = True
        if not processing_claim_is_current(
            delivery_context.conversation_id,
            delivery_context.generation,
            delivery_context.worker_id,
        ):
            delivery_context.stale_discarded = True
            print("[Conversacion] Botones obsoletos descartados por generation/lease.")
            return False
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(phone_number),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "¿Cómo preferís recibir tu compra?"},
            "footer": {"text": "Elegí una opción para seguir"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "fulfillment:shipping", "title": "Envío"}},
                {"type": "reply", "reply": {"id": "fulfillment:pickup", "title": "Retiro"}},
            ]},
        },
    }
    try:
        response = requests.post(
            f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages",
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        if delivery_context and (
            normalize_whatsapp_recipient(phone_number)
            == normalize_whatsapp_recipient(delivery_context.customer_phone)
        ):
            delivery_context.delivered = True
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando botones de entrega: {type(error).__name__}")
        return False


def send_whatsapp_template(phone_number: str, template_name: str, language: str, body_values=None) -> bool:
    """Send an approved Meta template, including outside the 24-hour window."""
    if _real_outbound_is_blocked("plantilla"):
        return False
    if not template_name:
        return False
    template = {"name": template_name, "language": {"code": language}}
    if body_values:
        template["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(value)} for value in body_values],
        }]
    try:
        response = requests.post(
            f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": normalize_whatsapp_recipient(phone_number),
                "type": "template",
                "template": template,
            },
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[WhatsApp] Plantilla HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando plantilla a WhatsApp: {type(error).__name__}")
        return False


def send_whatsapp_document(phone_number: str, document_url: str, filename: str, caption: str) -> bool:
    """Send a client-facing PDF through WhatsApp Cloud API using a stable URL."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(phone_number),
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption,
        },
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        print(f"[WhatsApp] Documento HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando documento a WhatsApp: {type(error).__name__}")
        return False


def send_isa_pending_notification(pending_count: int) -> bool:
    """Notify Isa once when the queue changes from empty to non-empty."""
    if not ISA_WHATSAPP_NUMBER:
        print("ERROR avisando a Isa: falta ISA_WHATSAPP_NUMBER")
        return False
    return send_escalacion_isa_template(ISA_WHATSAPP_NUMBER, pending_count)


def _business_reminder_time(now: datetime) -> bool:
    """Do not chase Isa at night; she can explicitly ask for a later reminder."""
    return (now.hour, now.minute) >= (10, 0) and (now.hour, now.minute) < (20, 30)


def run_isa_reminder_check(now: datetime = None) -> None:
    """Send at most two friendly, template-safe reminders per local day."""
    if not ISA_REMINDERS_ENABLED or not ISA_WHATSAPP_NUMBER:
        return
    now = now or datetime.now(ARGENTINA_TZ)
    snapshot = pending_reminder_snapshot()
    if not snapshot["count"]:
        return

    if claim_requested_isa_reminder(ISA_WHATSAPP_NUMBER):
        if not send_isa_pending_notification(snapshot["count"]):
            print("ERROR enviando recordatorio solicitado a Isa.")
        return

    if not _business_reminder_time(now) or isa_reminders_snoozed(ISA_WHATSAPP_NUMBER):
        return
    oldest = snapshot["oldest_created_at"]
    if not oldest:
        return
    age = now - oldest.astimezone(ARGENTINA_TZ)
    kind = "follow_up" if age >= timedelta(hours=2) else "gentle"
    if kind == "gentle" and age < timedelta(minutes=25):
        return
    if not claim_daily_isa_reminder(ISA_WHATSAPP_NUMBER, kind, now.date()):
        return
    if not send_isa_pending_notification(snapshot["count"]):
        release_daily_isa_reminder(ISA_WHATSAPP_NUMBER, kind, now.date())
        print("ERROR enviando recordatorio automático a Isa.")


def run_daily_operations_report(now: datetime = None) -> None:
    """Send the owner-approved 21:00 summary template once per Argentina day."""
    if not (
        DAILY_SUMMARY_ENABLED
        and ISA_WHATSAPP_NUMBER
        and DAILY_SUMMARY_TEMPLATE_NAME
    ):
        return
    now = now or datetime.now(ARGENTINA_TZ)
    if now.hour < 21:
        return
    report_day = now.date()
    try:
        if not claim_daily_operations_report(report_day):
            return
        summary = daily_operations_summary()
        delivered = send_whatsapp_template(
            ISA_WHATSAPP_NUMBER,
            DAILY_SUMMARY_TEMPLATE_NAME,
            DAILY_SUMMARY_TEMPLATE_LANGUAGE,
            [
                summary["conversations"],
                summary["approved_checkouts"],
                summary["paid_orders"],
                summary["pending"],
            ],
        )
        finish_daily_operations_report(report_day, delivered)
        if not delivered:
            print("ERROR enviando resumen diario de Fred.")
    except Exception as error:  # noqa: BLE001
        print("ERROR preparando resumen diario de Fred (tipo: {}).".format(type(error).__name__))


async def _isa_reminder_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_isa_reminder_check)
            await asyncio.to_thread(run_daily_operations_report)
        except Exception as error:  # noqa: BLE001
            print("ERROR en recordatorios de Isa (tipo: {}).".format(type(error).__name__))
        await asyncio.sleep(300)


@app.on_event("startup")
async def start_isa_reminders() -> None:
    global _reminder_task, _message_worker_task
    if ISA_REMINDERS_ENABLED and _reminder_task is None:
        _reminder_task = asyncio.create_task(_isa_reminder_loop())
    if DURABLE_MESSAGE_PROCESSING_ENABLED and _message_worker_task is None:
        _message_worker_task = asyncio.create_task(_durable_message_worker_loop())
        print("[Cola] Worker durable iniciado: {}".format(_message_worker_id))


@app.on_event("shutdown")
async def stop_isa_reminders() -> None:
    if _reminder_task:
        _reminder_task.cancel()
    if _message_worker_task:
        _message_worker_task.cancel()


def _is_isa_phone(phone_number: str) -> bool:
    return bool(ISA_WHATSAPP_NUMBER) and (
        normalize_whatsapp_recipient(phone_number)
        == normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER)
    )


def _format_ars(value) -> str:
    """Format a verified ARS amount for people, never for calculations."""
    try:
        return "${:,.0f}".format(Decimal(str(value))).replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return "a confirmar"


def _pending_action_text(action: dict) -> str:
    labels = {
        "human_handoff": "Clienta pidió hablar con Isa",
        "purchase_review": "Compra pendiente de confirmación",
        "special_sale_request": "Encargo o cotización pendiente de Isa",
        "bot_fallback": "Fred necesita confirmar una consulta",
    }
    customer_message = action["payload"].get("customer_message", "")
    text = (
        "Pendiente #{}\n{}\nCliente: {}\n{}".format(
            action["id"],
            labels.get(action["action_type"], action["action_type"]),
            action["customer_phone"],
            action["summary"],
        )
    )
    if customer_message:
        text += "\nMensaje: {}".format(customer_message)
    conversation_context = action["payload"].get("conversation_context")
    if conversation_context:
        text += "\n\nContexto reciente:\n" + "\n".join(
            "{}: {}".format(item.get("speaker", ""), item.get("body", ""))
            for item in conversation_context
        )
    sale_draft = action["payload"].get("sale_draft")
    if sale_draft:
        text += (
            "\n\nBorrador de venta"
            "\nProducto/cantidad: {}"
            "\nVariante: {}"
            "\nSubtotal productos: {}"
            "\nEntrega: {}"
            "\nEnvío: a confirmar"
            "\nTotal final: a confirmar"
            "\nCliente: {}"
            "\nEmail: {}"
            "\nPago: {}"
        ).format(
            sale_draft["items_status"],
            sale_draft.get("selected_variant", "a confirmar"),
            _format_ars(sale_draft.get("products_subtotal")),
            sale_draft["delivery_status"],
            sale_draft.get("customer_name", "a confirmar"),
            sale_draft.get("customer_email", "a confirmar"),
            sale_draft["payment_status"],
        )
    # WhatsApp allows only three buttons, so the help lives in the body: Isa
    # can ask in words instead of losing an action button to it.
    hint = "\n\nSi tenés dudas, escribime AYUDA y te explico qué hace cada opción."
    return text[:900 - len(hint)] + hint


def send_isa_pending_buttons(action: dict) -> bool:
    """Show one queued draft to Isa after she messages the bot."""
    if _real_outbound_is_blocked("tarjeta a Isa"):
        return False
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    action_id = action["id"]
    buttons = []
    if action["action_type"] == "purchase_review":
        # Dos decisiones y nada más: aprobar, o hablar directamente con la
        # clienta. No hay rechazo con motivo, ni relay, ni consulta.
        buttons = [
            {"type": "reply", "reply": {"id": "approve_checkout:{}".format(action_id), "title": "Aprobar"}},
            {"type": "reply", "reply": {"id": "contact_customer:{}".format(action_id), "title": "Hablar con cliente"}},
        ]
    elif action["action_type"] == "bot_fallback":
        buttons = [
            {"type": "reply", "reply": {"id": "reply_to_fred:{}".format(action_id), "title": "Responder y cerrar"}},
            {"type": "reply", "reply": {"id": "reply_keep_open:{}".format(action_id), "title": "Seguir conversando"}},
            {"type": "reply", "reply": {"id": "resume_bot:{}".format(action_id), "title": "Que siga Fred"}},
        ]
    elif action["action_type"] == "special_sale_request":
        buttons = [
            {"type": "reply", "reply": {"id": "send_special_conditions:{}".format(action_id), "title": "Enviar condiciones"}},
            {"type": "reply", "reply": {"id": "reply_to_fred:{}".format(action_id), "title": "Responder a Fred"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
        ]
    elif action["action_type"] == "human_handoff":
        if action.get("payload", {}).get("awaiting_isa_kind") == ISA_OWNS_KIND:
            # She already has the thread: what she needs now is a way out.
            buttons = [
                {"type": "reply", "reply": {"id": "return_to_fred:{}".format(action_id), "title": "Devolver a Fred"}},
                {"type": "reply", "reply": {"id": "close_consultation:{}".format(action_id), "title": "Cerrar consulta"}},
            ]
        else:
            buttons = [
                {"type": "reply", "reply": {"id": "reply_to_fred:{}".format(action_id), "title": "Responder y cerrar"}},
                {"type": "reply", "reply": {"id": "reply_keep_open:{}".format(action_id), "title": "Seguir conversación"}},
                {"type": "reply", "reply": {"id": "close_consultation:{}".format(action_id), "title": "Cerrar consulta"}},
            ]
    else:
        buttons = [
            {"type": "reply", "reply": {"id": "approve:{}".format(action_id), "title": "Tomar caso"}},
            {"type": "reply", "reply": {"id": "reject:{}".format(action_id), "title": "Volver a Fred"}},
            {"type": "reply", "reply": {"id": "view:{}".format(action_id), "title": "Ver contexto"}},
        ]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": _pending_action_text(action)},
            "action": {
                "buttons": buttons
            },
        },
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[Isa] HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando botones a Isa: {type(error).__name__}")
        return False


def send_next_pending_to_isa() -> bool:
    pending_actions = list_pending_actions(limit=1)
    if not pending_actions:
        return send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No tenés pendientes para revisar. 😊")
    return send_isa_pending_buttons(pending_actions[0])


def _queue_for_isa(
    conversation_id: int,
    customer_phone: str,
    action_type: str,
    summary: str,
    customer_message: str,
    conversation_context: list = None,
    sale_draft: dict = None,
) -> bool:
    """Create an escalation and try to reach Isa. Returns whether Isa was
    actually notified NOW.

    The case is always persisted first, so nothing is ever lost -- but Fred
    must not tell a customer "ya se lo pasé a Isa" when the WhatsApp send
    failed and Isa has no idea the case exists. Callers word their reply from
    this result. A False here means "registrado, Isa lo ve al abrir su cola",
    not "perdido".
    """
    pending_before = pending_action_count()
    payload = {"customer_phone": customer_phone, "customer_message": customer_message}
    if action_type == "purchase_review":
        # A draft is an internal checklist, not an order. Fields are populated
        # only later, after Isa reviews the conversation and explicitly approves.
        payload["sale_draft"] = sale_draft or {
            "status": "needs_isa_review",
            "items_status": "por confirmar",
            "delivery_status": "por confirmar",
            "payment_status": "por confirmar",
            "order_creation": "disabled",
        }
    # Every escalation type keeps the recent turns, not just purchase/special
    # cases: "clienta pidió hablar con Isa" and "Fred no está seguro" are the
    # most common escalations and Isa needs that same context to answer
    # without asking the customer to repeat everything.
    payload["conversation_context"] = [
        {
            "speaker": "Clienta" if item.get("role") == "user" else "Fred",
            "body": (item.get("content") or "")[:240],
        }
        for item in (conversation_context or [])[-6:]
        if item.get("content")
    ]
    action_id = create_pending_action(
        conversation_id=conversation_id,
        action_type=action_type,
        summary=summary,
        payload=payload,
    )
    # A pending question is asynchronous: Fred keeps helping the client while
    # Isa reviews it.  The only explicit pause is Isa pressing “Pausar a Fred”.
    # Previously every escalation set ESCALATED and silently ignored any later
    # customer message, which made normal support feel abandoned.
    set_conversation_state(conversation_id, "BOT")
    action = {
        "id": action_id,
        "action_type": action_type,
        "summary": summary,
        "payload": payload,
        "customer_phone": customer_phone,
    }
    # If Isa wrote recently, WhatsApp permits the detailed interactive card
    # immediately. Outside that window Meta rejects it, so fall back to the
    # approved template and wait for Isa to reply "ver".
    notified = False
    try:
        # The interactive card only lands inside Meta's 24h window, i.e. when
        # Isa is already active -- always worth trying, never spam.
        notified = bool(send_isa_pending_buttons(action))
        if not notified:
            # A transient failure (ConnectionError, a Meta hiccup) must not
            # leave Isa unaware of a case she could act on right now: inside
            # an open window a plain-text summary always gets through, and it
            # never depends on the approved template.
            notified = bool(send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "{}\n\nEscribime “ver” para abrirlo con los botones.".format(
                    _pending_action_text(action),
                ),
            ))
        if not notified and pending_before == 0:
            # Outside that window only the approved template can reach her, and
            # it is reserved for opening a queue that was empty so a busy hour
            # doesn't become one template per case.
            notified = bool(send_isa_pending_notification(pending_before + 1))
    except Exception as error:  # noqa: BLE001
        print("ERROR notificando a Isa (tipo: {}).".format(type(error).__name__))
    print("[Isa] Pendiente #{} creado ({}). Notificada: {}.".format(
        action_id, action_type, "sí" if notified else "NO",
    ))
    return notified


# ============================================================
# FRED CORE — one explicit state machine, one source of truth.
#
# Every conversation has a persisted `mode` (CHAT/MENU/CHECKOUT/TRACKING/
# ISA) plus structured fields (active_product_id, quantity, checkout_step,
# ...) in fred_core_state. This is the ONLY place conversational state is
# read or written for routing purposes -- never by re-reading Fred's own
# last message ("Fred preguntó X, entonces probablemente..."). CHAT is the
# only mode the model participates in; MENU/CHECKOUT/TRACKING/ISA execute
# deterministically from the persisted fields alone.
# ============================================================

# --- pending_intent: qué acción quedó esperando una respuesta -------------
#
# Cuando Fred hace una pregunta cuya respuesta ejecuta o modifica algo, la
# intención se persiste ANTES de enviar el mensaje. Sin esto un "sí" no tiene
# a qué referirse y termina reinterpretado como un mensaje nuevo (el bug real:
# "¿avanzamos?" -> "sí" -> Fred volvía a buscar productos).
PENDING_CONFIRM_PURCHASE_DRAFT = "CONFIRM_PURCHASE_DRAFT"

# Afirmación/negación en el sentido humano, no una lista cerrada de frases:
# esto sólo resuelve los casos baratos y obvios. Cuando el mensaje es más
# complejo que esto, no se fuerza ninguna interpretación acá -- sigue su curso
# normal y el modelo lo interpreta en contexto.
_AFFIRMATION_RE = re.compile(
    r"^(?:s[ií]+|sip|sii+|dale|ok(?:ey|ay)?|oka|listo|perfecto|genial|barbaro|"
    r"buenisimo|dale\s+si|si\s+dale|confirmo|confimo|confirmar|dale\s+confirmo|"
    r"avancemos|avanza|avanzemos|hagamoslo|hagamosla|de\s+una|obvio|claro|"
    r"si\s+por\s+favor|si\s+porfa|correcto|exacto|asi\s+es|va|vale)"
    r"(?:\s*[,.!]*\s*(?:dale|gracias|porfa|por\s+favor|listo|ok))?[.!]*$"
)
_NEGATION_RE = re.compile(
    r"^(?:no+|nop|mejor\s+no|no\s+gracias|cancela(?:lo|r)?|cancelo|dejalo|"
    r"dejemoslo|no\s+sigo|no\s+quiero|olvidalo|despues|mas\s+tarde)[.!]*$"
)


def _reads_as_affirmation(text: str) -> bool:
    """A plain human "yes" to whatever was just asked."""
    return bool(_AFFIRMATION_RE.match(_normalized_text(text).strip()))


def _reads_as_negation(text: str) -> bool:
    """A plain human "no"/"cancel" to whatever was just asked."""
    return bool(_NEGATION_RE.match(_normalized_text(text).strip()))


def isa_contact_number() -> str:
    """Isa's own WhatsApp, shown to the customer so they write to her directly.
    There is no relay: Fred hands over the contact and steps out."""
    raw = re.sub(r"\D", "", ISA_WHATSAPP_NUMBER or "")
    return "+{}".format(raw) if raw else ""


def _isa_direct_contact_reply(lead: str) -> str:
    """One sentence plus Isa's number. No pending case, no notification, no
    consultation -- the customer simply talks to her."""
    number = isa_contact_number()
    if not number:
        return lead
    return "{} Podés escribirle directamente acá: {}".format(lead, number)


# A handoff carries two different strings and they must never be confused: the
# "summary" is written for Isa and for the audit trail (it names topics,
# routing sources and verifiers), while what the customer reads is chosen HERE,
# from the deterministic reason alone. Rendering a summary as customer copy is
# what produced replies like "El topic aprobado requiere revisión de Isa para
# este caso" -- an internal audit field used as conversation.
_ISA_HANDOFF_LEADS = {
    "special_sale_request": (
        "Este pedido lo prepara Isa personalmente para darte el precio y el "
        "plazo reales."
    ),
    "human_request": "Dale, lo mejor es que lo veas directamente con Isa.",
    "purchase_intent": "Para cerrar la compra te acompaña Isa.",
    "unable_to_verify": (
        "Esto no lo tengo confirmado y prefiero no darte un dato incorrecto, "
        "así que lo mejor es que lo veas con Isa."
    ),
}
_ISA_HANDOFF_DEFAULT_LEAD = "Esto prefiero que lo veas con Isa."


def _isa_handoff_lead(reason: Optional[str]) -> str:
    """The customer-facing sentence for a handoff. Only ever derived from the
    deterministic reason -- never from a summary, however well it reads."""
    return _ISA_HANDOFF_LEADS.get(str(reason or ""), _ISA_HANDOFF_DEFAULT_LEAD)


def _isa_handoff_confirmation(notified: bool) -> str:
    """Say what actually happened. "Se lo pasé a Isa" is a claim about a
    message that either arrived or didn't; when it didn't, the case is still
    safely registered and saying so is both true and reassuring."""
    if notified:
        return (
            "Listo, se lo pasé a Isa junto con el contexto de la conversación para "
            "que no tengas que repetir todo. 😊"
        )
    return (
        "Tu consulta quedó registrada para que la revise Isa, con todo el contexto "
        "de la conversación así no tenés que repetir nada. Apenas la vea, te "
        "respondo por acá. 😊"
    )


def _remember_pending_intent(conversation_id: int, intent: Optional[str]) -> None:
    """Persist (or clear) what Fred just asked. Never blocks the reply: if the
    write fails the customer still gets answered, they just lose the shortcut
    of a bare "sí" resolving it."""
    try:
        save_fred_core_state(conversation_id, pending_intent=intent)
    except Exception as error:  # noqa: BLE001
        print("ERROR guardando intención pendiente (tipo: {}).".format(type(error).__name__))


FALLBACK_MENU_MARKER = "¿Cómo querés seguir?"
ORDER_NUMBER_PROMPT_TEXT = "¿Cuál es tu número de orden?"
_MENU_SELECTION_RE = re.compile(r"^(?:opcion\s*)?([1-4])\.?$")
_BARE_ORDER_NUMBER_RE = re.compile(r"^\D*(\d{3,})\D*$")
# A generic, non-product-discovery hedge (a Knowledge question Fred can't
# answer confidently, no obligation/escalation already matched it). Narrow
# and literal on purpose: this only ever fires on the model's *own* final
# wording, guarded by "not already escalating", so a false positive just
# means an extra offer of the same menu, never a lost real answer.
_HEDGE_PHRASES = (
    "no tengo confirmado", "no lo tengo confirmado", "no tengo esa informacion",
    "no tengo ese dato", "no estoy segura de eso", "no estoy seguro de eso",
    "no sabria decirte", "no cuento con esa informacion",
)


def _looks_like_a_hedge(text: str) -> bool:
    normalized = _normalized_text(text)
    return any(phrase in normalized for phrase in _HEDGE_PHRASES)


# The bare presence of "mi pedido"/"mi orden" is real evidence for the
# knowledge_rag order_tracking gate (a required check inside an LLM turn),
# but it is too weak on its own to bypass the model entirely -- "mi pedido
# llegó perfecto, gracias" would wrongly get treated as a status request.
# This stricter subset is safe to trigger TRACKING mode with zero LLM rounds.
_STRONG_TRACKING_TRIGGER_RE = re.compile(
    r"donde\s+esta\s+mi\s+(?:compra|pedido|orden)|no\s+me\s+lleg[oa]\b|"
    r"\btracking\b|\bseguimiento\b|numero\s+de\s+(?:orden|pedido)|"
    r"consultar\s+mi\s+(?:compra|pedido|orden)|"
    r"estado\s+de\s+mi\s+(?:compra|pedido|orden)|"
    r"saber\s+(?:de|sobre)\s+mi\s+(?:compra|pedido|orden)|"
    r"rastre\w*\s+mi\s+(?:compra|pedido|orden)"
)


def _render_fallback_menu(active_product_name: str = "") -> str:
    buy_label = "Comprar {}".format(active_product_name) if active_product_name else "Comprar un producto"
    return (
        "No tengo información segura para responder eso todavía.\n\n"
        "{}\n\n"
        "1. Ver opciones de productos\n"
        "2. {}\n"
        "3. Consultar un pedido\n"
        "4. Hablar con Isa"
    ).format(FALLBACK_MENU_MARKER, buy_label)


def _most_recent_customer_message(history: list) -> str:
    return next(
        (
            str(item.get("content") or "").strip()
            for item in reversed(history or [])
            if item.get("role") == "user" and str(item.get("content") or "").strip()
        ),
        "",
    )


def _extract_menu_selection(text: str) -> Optional[str]:
    match = _MENU_SELECTION_RE.match(_normalized_text(text).strip())
    return match.group(1) if match else None


_ASK_WHICH_PRODUCT = (
    "¡Dale! 😊 ¿Qué producto estás buscando? Si me pasás el nombre como figura "
    "en la tienda, lo ubico enseguida."
)


def _quantity_in_message(normalized_message: str) -> str:
    match = re.search(r"\b(\d{1,3})\s+\w{3,}", normalized_message)
    return match.group(1) if match else ""


# "quiero 4" -- a number with nothing after it only means a quantity when the
# conversation already has a product for it to attach to.
_BARE_QUANTITY_RE = re.compile(r"\b(\d{1,3})\b(?!\s*\w)")
# Wanting a thing that has just been named, as opposed to wanting to do
# something ("quiero pasar", "quiero saber").
_WANTING_RE = re.compile(r"\b(quiero|quisiera|necesito|me\s+interesa[n]?)\s+(?!\w*ar\b|\w*er\b|\w*ir\b)")


def _bare_quantity_in_message(normalized_message: str) -> str:
    match = _BARE_QUANTITY_RE.search(normalized_message)
    return match.group(1) if match else ""


def _current_turn_purchase_evidence(normalized_message: str) -> str:
    """What in THIS message says the customer is buying, if anything.

    Returns "explicit" when the message says so on its own terms, "reference"
    when it points back at something already on the table, and "" when there
    is no purchase evidence at all -- which is the case for "quiero pasar por
    el showroom" and "quiero saber los horarios". "quiero" is not evidence:
    it is the most common verb in the language and appears in policy
    questions, order questions and pleasantries alike.

    Deliberately reads only the message. Persisted state is never consulted
    here, because a product pinned in an earlier turn says nothing about what
    this turn is about.
    """
    if _UNAMBIGUOUS_PURCHASE_VERB_RE.search(normalized_message):
        return "explicit"
    if _named_catalog_product(normalized_message, product_lexicon()):
        # A product named here, with a quantity or a wanting verb next to it.
        if (
            _quantity_in_message(normalized_message)
            or _bare_quantity_in_message(normalized_message)
            or _WANTING_RE.search(normalized_message)
        ):
            return "explicit"
        return ""
    # Nothing named, but the message points at something: "quiero 4",
    # "me llevo dos", "de esas". Only a reference -- it needs an antecedent.
    if (
        _quantity_in_message(normalized_message)
        or _bare_quantity_in_message(normalized_message)
        or _ANAPHORIC_REFERENCE_RE.search(normalized_message)
    ):
        return "reference"
    return ""


def _isa_scope_handoff(message_text: str, core_state: dict) -> str:
    """The two things Fred no longer does, answered without spending anything.

    Advice ("¿cuál me recomendás?") and closing a sale ("quiero 4 Isabel I")
    are Isa's. Recognising them here, from the message alone, is what removes
    the catalog search, the live verification and the model rounds from turns
    whose destination was never Fred.

    Returns the reply to send, or "" to let the turn continue normally.

    Purchase intent needs a commercial OBJECT to count: "quiero pasar por el
    showroom" is not a purchase, and treating it as one is what sent a policy
    question to the store. When the intent is real but no product is named
    ("quiero 4 pestañas"), Fred asks which one instead of handing over a
    request Isa cannot act on.
    """
    normalized = _knowledge_normalise(message_text)
    verdict = classify_turn_data_requirement(
        message_text,
        product_lexicon=product_lexicon(),
        product_lexicon_available=product_lexicon_available(),
    )
    intent = verdict.get("intent")

    if intent == INTENT_ADVICE_REQUEST:
        print("[FredScope] asesoramiento -> Isa (sin catálogo ni modelo).")
        return _isa_direct_contact_reply(
            "Para recomendarte la opción que mejor te queda, prefiero que te "
            "asesore Isa directamente."
        )

    if intent != INTENT_PURCHASE_INTENT:
        return ""

    # THE CURRENT MESSAGE DECIDES. A conversation can legitimately carry an
    # active product from an hour ago and then change the subject entirely --
    # "quiero pasar por el showroom" with Isabel I still pinned was handed to
    # Isa as a purchase, because "quiero" plus a stale active_product looked
    # like buying. active_product is auxiliary context: it can supply the
    # IDENTITY of something this turn already refers to, and it is never on
    # its own evidence that this turn is about buying anything.
    evidence = _current_turn_purchase_evidence(normalized)
    if not evidence:
        return ""

    # Only now, with this turn established as commercial, may context supply
    # the identity: "me llevo dos" means the product already on the table.
    # The order matters -- evidence first, identity second. Reversing it is
    # exactly what turned a showroom question into a sale.
    active_product = (core_state or {}).get("active_product_name") or ""
    named_here = _named_catalog_product(normalized, product_lexicon())
    if not (named_here or active_product):
        print("[FredScope] compra sin producto identificado -> se pregunta cuál.")
        return _ASK_WHICH_PRODUCT

    # Hand over what the customer already said, so Isa does not re-ask it.
    quantity = _quantity_in_message(normalized) or _bare_quantity_in_message(normalized)
    detail = " ".join(part for part in (
        "{} unidades de".format(quantity) if quantity else "",
        active_product or message_text.strip(),
    ) if part).strip()
    print("[FredScope] intención de compra -> Isa (sin checkout ni botón).")
    return _isa_direct_contact_reply(
        "¡Genial! Para cerrar la compra ({}) te paso con Isa, que la coordina "
        "directamente.".format(detail[:120])
    )


# Fred asking for an order number is a QUESTION, and the answer to it is the
# next message. That continuity was missing: the deterministic prompt sets
# mode=TRACKING, but when the model asked in its own words nothing recorded
# that a number was expected, so a bare "6295" arrived as a fresh turn. The
# retrieval query was then rebuilt from surrounding history and picked up an
# unrelated complaint from earlier in the conversation ("el protector solar
# llegó abierto"), which pulled damaged-product Knowledge into an order lookup.
_ASKED_FOR_ORDER_NUMBER_RE = re.compile(
    r"n[úu]mero\s+de\s+(?:orden|pedido)|nro\.?\s+de\s+(?:orden|pedido)|"
    r"c[óo]digo\s+de\s+(?:orden|pedido)|qu[ée]\s+pedido\s+es"
)


def _fred_just_asked_for_order_number(prior_history: list) -> bool:
    """Did Fred's LAST message ask for an order number?

    Only the last one: an order-number request from earlier in the thread has
    already been answered or abandoned, and treating a number as an answer to
    a stale question is the same class of bug this fixes.
    """
    last_assistant = next(
        (
            str(item.get("content") or "")
            for item in reversed(prior_history or [])
            if item.get("role") == "assistant"
        ),
        "",
    )
    return bool(_ASKED_FOR_ORDER_NUMBER_RE.search(_normalized_text(last_assistant)))


def _extract_bare_order_number(text: str) -> Optional[str]:
    """Once Fred already asked "¿cuál es tu número de orden?", the reply is
    almost always just the number by itself -- no "pedido"/"orden" keyword
    to anchor on, unlike extract_order_number's general-purpose regex."""
    match = _BARE_ORDER_NUMBER_RE.match(text.strip())
    return match.group(1) if match else None


def _deliver_flow_reply(customer_phone: str, conversation_id: int, reply: str) -> None:
    if reply == "__FULFILLMENT_BUTTONS__":
        delivered = send_customer_fulfillment_buttons(customer_phone)
        reply_to_store = "¿Cómo preferís recibir tu compra? [Envío / Retiro]"
    else:
        delivered = send_whatsapp_text(customer_phone, reply)
        reply_to_store = reply
    if delivered:
        record_bot_message(conversation_id, reply_to_store)


def _fred_core_active_product_fields(candidate: dict) -> dict:
    """Map a verified {sku, product_name, variant, unit_price} candidate to
    fred_core_state's column names, the one shape every mode writes through."""
    return {
        "active_product_id": candidate.get("sku") or None,
        "active_product_name": candidate.get("product_name") or None,
        "active_sku": candidate.get("sku") or None,
        "active_variant": candidate.get("variant") or None,
        "unit_price": candidate.get("unit_price"),
    }


# --- mode=TRACKING ----------------------------------------------------

def log_order_live(result: dict) -> None:
    """What Tiendanube actually returned, with no personal data.

    Deliberately only status-shaped fields: never a name, email, address,
    phone or document. Reading this line next to [FredDecision] is what makes
    "Fred said the wrong thing about my order" diagnosable.
    """
    try:
        print(
            "[OrderLive] order={} payment_status={} shipping_status={} "
            "shipping_type={} fulfillment_status={} carrier={} tracking={}".format(
                result.get("order_number") or "none",
                result.get("payment_status") or "none",
                result.get("shipping_status") or "none",
                result.get("shipping_type") or "none",
                result.get("fulfillment_status") or "none",
                result.get("carrier") or "none",
                "yes" if result.get("tracking") else "no",
            )
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando estado live del pedido (tipo: {}).".format(type(error).__name__))


# Preparation and delivery windows are Knowledge's, not this module's. They are
# quoted rather than computed: no exact date is ever promised.
_PREPARATION_WINDOW = "El plazo habitual de preparación es de 24 a 72 horas hábiles desde que se acredita el pago."
_DELIVERY_WINDOW = "Una vez despachado, la entrega suele demorar entre 1 y 5 días hábiles."
_WATCH_YOUR_EMAIL = "Te vamos a avisar por correo cuando avance."


def _render_order_status_reply(result: dict) -> str:
    """Deterministic status text built from the fulfillment Tiendanube
    returned, never from payment alone.

    The states are the ones the Tiendanube UI shows, measured against 40 real
    orders (UNPACKED / PACKED / DISPATCHED / DELIVERED, each ship or pickup).
    A paid order is not a packed one, and a packed one is not collectable:
    each stage says only what it knows, and pickup coordination stays with the
    approved policy rather than being inferred from a status.
    """
    order_number = result.get("order_number")
    fulfillment = str(result.get("fulfillment_status") or "").upper()
    is_pickup = str(result.get("shipping_type") or "").lower() == "pickup"
    tracking = result.get("tracking")
    carrier = result.get("carrier")
    payment_status = str(result.get("payment_status") or "").lower()

    # Payment first: nothing downstream has happened yet if it has not cleared.
    if payment_status and payment_status not in ("paid", "approved"):
        return (
            "Tu pedido #{} todavía no tiene el pago acreditado (estado: {}). "
            "En cuanto se acredite, empezamos a prepararlo."
        ).format(order_number, payment_status)

    if fulfillment == "DELIVERED":
        if is_pickup:
            return "Tu pedido #{} figura como retirado. ¡Gracias! 😊".format(order_number)
        return "Tu pedido #{} figura como entregado. ¡Gracias! 😊".format(order_number)

    if fulfillment == "DISPATCHED":
        text = "Tu pedido #{} ya fue despachado".format(order_number)
        text += " con {}.".format(carrier) if carrier else "."
        if tracking:
            text += " Número de seguimiento: {}.".format(tracking)
        text += " " + _DELIVERY_WINDOW
        return text

    if fulfillment == "PACKED":
        if is_pickup:
            # Deliberately NOT "listo para retirar": PACKED+pickup has not been
            # observed against the Tiendanube UI yet, so this says only what is
            # certain (it is packed) and asks the customer to wait for the
            # confirmation rather than travel on a guess.
            return (
                "Tu pedido #{} ya está empaquetado. Antes de acercarte, esperá "
                "la confirmación por correo así no hacés el viaje en vano."
            ).format(order_number)
        return (
            "Tu pedido #{} ya está empaquetado y casi listo para salir. "
            "Estate atenta al correo, que ahí te avisamos cuando se despache."
        ).format(order_number)

    if fulfillment == "UNPACKED":
        return (
            "Tu pedido #{} todavía está en preparación y aún no salió. {} {}"
        ).format(order_number, _PREPARATION_WINDOW, _WATCH_YOUR_EMAIL)

    # No fulfillment record yet: say what is true and nothing more.
    return (
        "Tu pedido #{} está en preparación. {} {}"
    ).format(order_number, _PREPARATION_WINDOW, _WATCH_YOUR_EMAIL)


# Wanting to collect an order, in the customer's own words.
_PICKUP_REQUEST_RE = re.compile(
    r"\bretir\w+|\bpaso\s+a\s+(?:buscar|retirar)|\bpasar\s+a\s+(?:buscar|retirar)|"
    r"\blo\s+busco\b|\bir\s+a\s+buscar\b"
)


def _pickup_requested(message_text: str, prior_history: list) -> bool:
    """Did the customer ask to COLLECT the order, here or in the message that
    started this tracking exchange? The trigger usually sits one turn back
    ("quiero retirar un pedido" -> "¿cuál es tu número de orden?" -> "6295"),
    so the customer's own last two messages are what count."""
    recent = [message_text] + [
        str(item.get("content") or "")
        for item in reversed(prior_history or [])
        if item.get("role") == "user"
    ][:2]
    return any(_PICKUP_REQUEST_RE.search(_normalized_text(text)) for text in recent)


def _pickup_next_step() -> str:
    """The approved pickup policy, which starts with a reservation.

    A live order status is NOT authorisation to come and collect: "paid and
    being prepared" says the payment cleared, not that anything is packed,
    at the showroom, or that there is a slot. Production inferred exactly
    that ("está pagado y en preparación, no hay problema para retirarlo").
    Per knowledge/procedures/pickups.md the first requirement is a booking,
    and availability is never confirmed without Isa's approval.
    """
    return (
        " Para el retiro hace falta reservar día y horario: decime cuándo te "
        "quedaría cómodo y lo coordinamos con Isa antes de que vengas. 😊"
    )


def _fred_core_lookup_order(
    conversation_id: int, customer_phone: str, order_number: str, prior_history: list,
    pickup_requested: bool = False,
) -> str:
    """Zero LLM rounds: real Tiendanube lookup, deterministic reply, and a
    deterministic handoff (never left to model judgment) when the order
    doesn't exist or its own data is inconsistent. Always returns mode to
    CHAT: a pending Isa case (if any) is tracked by pending_actions, not by
    holding this conversation in TRACKING."""
    try:
        result = get_order_status(order_number)
    except Exception as error:  # noqa: BLE001
        print("ERROR consultando get_order_status (tipo: {})".format(type(error).__name__))
        save_fred_core_state(conversation_id, mode="CHAT", order_number=order_number)
        return _isa_direct_contact_reply(
            "No pude consultar tu pedido en este momento."
        )

    log_order_live(result)

    outcome = DynamicCheckOutcome(
        fact="order_status", verifier="get_order_status", status="completed", result=result,
    )
    escalation_reason = _order_status_needs_isa((outcome,))
    if escalation_reason:
        summaries = {
            "order_not_found": "La orden {} no existe en Tiendanube.".format(order_number),
            "order_status_contradiction": (
                "El pedido {} está marcado enviado/entregado pero no tiene "
                "tracking registrado."
            ).format(order_number),
        }
        summary = summaries[escalation_reason]
        save_fred_core_state(conversation_id, mode="CHAT", order_number=order_number)
        if escalation_reason == "order_not_found":
            return _isa_direct_contact_reply(
                "Busqué el pedido {} y no me aparece en el sistema.".format(order_number)
            )
        return _isa_direct_contact_reply(
            "El estado de tu pedido {} tiene una inconsistencia que conviene "
            "que revise Isa.".format(order_number)
        )

    save_fred_core_state(conversation_id, mode="CHAT", order_number=order_number)
    reply = _render_order_status_reply(result)
    # The status is reported first and on its own terms; the pickup policy is
    # appended, never derived from it.
    return reply + _pickup_next_step() if pickup_requested else reply


def _fred_core_handle_tracking(
    conversation_id: int, customer_phone: str, message_text: str, prior_history: list,
):
    """Waiting for an order number is waiting, not holding the conversation.

    A message that answers the question gets looked up. A message that plainly
    does not -- "cancelar", "hola", "quiero pasar al showroom" -- releases
    TRACKING and returns None, which makes the caller reprocess THIS SAME
    message as a normal CHAT turn. Nobody has to send it twice.

    TRACKING used to answer anything non-numeric with "decime sólo el número
    de orden", which meant a customer who changed the subject could not get
    out: every new question was answered with the same request.
    """
    order_number = extract_order_number(message_text) or _extract_bare_order_number(message_text)
    if not order_number:
        print("[FredCore] TRACKING liberado: el mensaje no es un número de orden.")
        save_fred_core_state(conversation_id, mode="CHAT")
        return None
    return _fred_core_lookup_order(
        conversation_id, customer_phone, order_number, prior_history,
        pickup_requested=_pickup_requested(message_text, prior_history),
    )


# --- mode=MENU, opción 1: ver productos ---------------------------------

def _fred_core_search_products(query: str) -> tuple:
    """Real catalog search, up to 3 relevant verified candidates. Returns
    (reply_text, single_candidate_or_None) -- the caller adopts the
    candidate as active_product only when exactly one came back."""
    if not query:
        return (
            "Contame qué tipo de producto buscás (por ejemplo pestañas, algún "
            "look en particular) y te muestro opciones reales. 😊"
        ), None
    try:
        results = search_available_products(query)
    except Exception as error:  # noqa: BLE001
        print("ERROR buscando catálogo en flujo de productos (tipo: {})".format(type(error).__name__))
        results = []
    candidates = _filter_relevant_candidates(_collect_turn_candidates(results, {}), query)
    if not candidates:
        return (
            "No tengo opciones verificadas para mostrarte todavía. ¿Me contás "
            "un poco más de lo que buscás? También puedo pasarte con Isa. 😊"
        ), None
    lines = ["Estas son las opciones que encontré:"]
    for index, candidate in enumerate(candidates[:3], start=1):
        details = []
        price = _format_price(candidate.get("price"))
        if price:
            details.append(price)
        if candidate.get("status") == "in_stock":
            details.append("disponible")
        elif candidate.get("status") == "out_of_stock":
            details.append("sin stock por ahora")
        name = candidate.get("product_name") or "Opción"
        suffix = " — {}".format(", ".join(details)) if details else ""
        lines.append("{}. {}{}".format(index, name, suffix))
    text = "\n".join(lines)
    if len(candidates) == 1:
        text += "\n\n¿Querés que avancemos con esta? 😊"
        return text, candidates[0]
    text += "\n\nContame cuál te interesa y seguimos. 😊"
    return text, None


# --- mode=CHECKOUT, opción 2: comprar -----------------------------------

def _fred_core_enter_checkout(
    conversation_id: int, customer_phone: str, core_state: dict,
    quantity: Optional[int] = None, message_text: str = "",
) -> str:
    """CHECKOUT is anchored to Fred Core's OWN active_product -- re-verified
    live here, never re-derived from whatever tool call happens to run this
    turn. This is the concrete fix for a stale/wrong product leaking into a
    purchase: there is exactly one place that decides which product a
    checkout is about, and it is this field, not a fresh guess."""
    active_sku = core_state.get("active_sku")
    active_name = core_state.get("active_product_name")
    if not active_sku:
        save_fred_core_state(conversation_id, mode="CHECKOUT", quantity=quantity)
        if active_name:
            # The product itself is known (e.g. it has more than one live
            # variant, so _live_product_candidate couldn't pin one SKU down).
            # Keep the name and quantity instead of restarting the checkout
            # from a blank slate -- the exact variant/price gets "a
            # confirmar" in the summary for Isa to resolve, same as any
            # other field that isn't verified yet.
            reply = _start_sales_intake(
                conversation_id,
                {"product_name": active_name, "sku": "", "variant": "", "unit_price": None},
                quantity=quantity or 0,
            )
        else:
            reply = _start_sales_intake(conversation_id, quantity=quantity or 0)
        if message_text:
            complete_summary = _apply_sale_details_from_same_message(conversation_id, message_text)
            if complete_summary:
                reply = complete_summary
        return reply
    try:
        fresh = get_stock(active_sku)
    except Exception as error:  # noqa: BLE001
        print("ERROR revalidando producto activo para checkout (tipo: {}).".format(type(error).__name__))
        fresh = {}
    if not fresh.get("found") or fresh.get("status") != "in_stock":
        return (
            "Recién revisé de nuevo y {} ya no tiene stock confirmado. "
            "¿Buscamos otra opción?"
        ).format(core_state.get("active_product_name") or "esa opción")
    candidate = {
        "product_name": fresh.get("product_name") or core_state.get("active_product_name"),
        "sku": active_sku,
        "variant": fresh.get("variant") or core_state.get("active_variant") or "",
        "unit_price": fresh.get("price"),
    }
    save_fred_core_state(
        conversation_id, mode="CHECKOUT", quantity=quantity,
        **_fred_core_active_product_fields(candidate),
    )
    reply = _start_sales_intake(conversation_id, candidate, quantity=quantity or 0)
    if message_text:
        # The same message may already carry delivery + contact details
        # ("quiero 2, envío, nombre, email") -- go straight to the summary
        # instead of asking for a field the customer already gave.
        complete_summary = _apply_sale_details_from_same_message(conversation_id, message_text)
        if complete_summary:
            reply = complete_summary
    return reply


def _fred_core_handle_checkout(
    conversation_id: int, customer_phone: str, message_text: str, prior_history: list,
) -> Optional[str]:
    """Delegates the turn-by-turn missing-field logic to the existing,
    already-tested sales_intakes machinery (product/quantity/fulfillment/
    customer/confirmation/Isa approval) unchanged -- Fred Core only
    guarantees the intake was anchored to the right product at entry, and
    mirrors the resulting step for reporting. Returns None either when the
    intake decided this message is about a different product entirely (its
    own "discovery outranks an unfinished checkout" rule -- releases the
    checkout) or when it's a genuine question unrelated to the pending field
    (preserves the checkout exactly as-is): either way the caller
    re-processes this same message as a normal CHAT turn."""
    intake = get_active_sales_intake(conversation_id)
    if not intake:
        # The intake was already resolved/cancelled by another path (e.g. a
        # duplicate webhook); nothing left to do here.
        reset_fred_core_checkout(conversation_id)
        return None
    handled = _handle_sales_intake(
        conversation_id, customer_phone, message_text, intake, prior_history,
    )
    if handled is None:
        # An interruption, not a different product: nothing about the
        # checkout changes, so the next message still resumes it.
        return None
    if not handled:
        reset_fred_core_checkout(conversation_id)
        save_fred_core_state(
            conversation_id, active_product_id=None, active_product_name=None,
            active_sku=None, active_variant=None, unit_price=None,
        )
        return None
    refreshed = get_active_sales_intake(conversation_id)
    if refreshed:
        save_fred_core_state(
            conversation_id,
            quantity=refreshed.get("quantity"),
            delivery_method=refreshed.get("fulfillment"),
            customer_name=refreshed.get("customer_name"),
            customer_email=refreshed.get("customer_email"),
            checkout_step=refreshed.get("status"),
        )
    else:
        # Confirmed (moved to ready_for_isa) or cancelled: no longer active.
        reset_fred_core_checkout(conversation_id)
    return "__HANDLED_NO_REPLY__"


# --- mode=ISA, opción 4: hablar con Isa ----------------------------------

def _fred_core_run_isa_handoff(
    conversation_id: int, customer_phone: str, prior_history: list, core_state: dict,
    summary: str = "",
) -> str:
    """Hand over Isa's contact. No consultation, no pending case, no relay:
    the customer writes to her directly and Fred steps out of the way."""
    save_fred_core_state(conversation_id, mode="CHAT")
    return _isa_direct_contact_reply(
        "Para esto es mejor que lo veas directamente con Isa."
    )


# --- mode=MENU ------------------------------------------------------------

def _fred_core_handle_menu(
    conversation_id: int, customer_phone: str, message_text: str, core_state: dict, prior_history: list,
) -> Optional[str]:
    """MENU only ever consumes an explicit 1-4 selection. Anything else is a
    real question, not a broken menu reply -- release back to CHAT instead of
    re-showing the same menu and silently refusing to call the model. The
    menu is a suggestion offered once, never a mode that holds the
    conversation hostage."""
    selection = _extract_menu_selection(message_text)
    if selection == "1":
        reply, resolved = _fred_core_search_products(_most_recent_customer_message(prior_history))
        fields = {"mode": "CHAT"}
        if resolved:
            fields.update(_fred_core_active_product_fields(resolved))
        save_fred_core_state(conversation_id, **fields)
        return reply
    if selection == "2":
        return _fred_core_enter_checkout(conversation_id, customer_phone, core_state)
    if selection == "3":
        save_fred_core_state(conversation_id, mode="TRACKING")
        return ORDER_NUMBER_PROMPT_TEXT
    if selection == "4":
        return _fred_core_run_isa_handoff(
            conversation_id, customer_phone, prior_history, core_state,
            "La clienta eligió hablar con Isa desde el menú.",
        )
    save_fred_core_state(conversation_id, mode="CHAT")
    return None


def _fred_core_dispatch(
    mode: str, conversation_id: int, customer_phone: str, message_text: str,
    core_state: dict, prior_history: list,
) -> Optional[str]:
    """switch(mode): the only place that decides which deterministic action
    runs. Returns the reply text, the "no reply, already delivered
    upstream" sentinel, or None to fall back to CHAT processing for this
    same message -- MENU does this for anything that isn't a 1-4 selection,
    and CHECKOUT does it when the message is about a different product."""
    if mode == "MENU":
        # MENU ya no existe en el runtime. Una conversación vieja que quedó
        # ahí se corrige sola y sigue como CHAT normal.
        save_fred_core_state(conversation_id, mode="CHAT")
        return None
    if mode == "CHECKOUT":
        return _fred_core_handle_checkout(conversation_id, customer_phone, message_text, prior_history)
    if mode == "TRACKING":
        return _fred_core_handle_tracking(conversation_id, customer_phone, message_text, prior_history)
    if mode == "ISA":
        # Transient by design (see _fred_core_run_isa_handoff): if a
        # conversation is ever found here, it is stale state left over from
        # before this executed -- correct it and treat this message as CHAT.
        save_fred_core_state(conversation_id, mode="CHAT")
        return None
    return None


def _customer_escalation_type(message_text: str, has_bot_history: bool) -> str:
    """Recognize direct human handoffs; sales intake handles purchase intent."""
    normalized = message_text.lower()
    if re.search(r"\bno\b.{0,30}\b(quiero|interesa|comprar|proceder)\b", normalized):
        return ""
    human_patterns = (
        r"hablar con isa",
        r"pasame con isa",
        r"quiero a isa",
        r"quiero hablar con una persona",
        r"hablar con alguien",
    )
    if any(re.search(pattern, normalized) for pattern in human_patterns):
        return "human_handoff"

    purchase_patterns = (
        r"\blo quiero\b",
        r"\bme lo llevo\b",
        r"\bquiero comprar\b",
        r"\bquiero hacer el pedido\b",
        r"\bproceder con la compra\b",
    )
    # Con la ficha de venta activa, no saltamos el catálogo ni molestamos a Isa:
    # el agente debe identificar/verificar el producto y pedir solo los datos
    # faltantes. El pase a Isa ocurre recién después de la confirmación final.
    if (
        has_bot_history
        and not SALES_INTAKE_ENABLED
        and any(re.search(pattern, normalized) for pattern in purchase_patterns)
    ):
        return "purchase_review"
    return ""


def _is_special_sale_context(message_text: str, prior_history: list) -> bool:
    """Backward-compatible wrapper around the shared pure routing policy."""
    return legacy_special_sale_context(message_text, prior_history)


def _needs_purchase_clarification(message_text: str, prior_history: list) -> bool:
    """Avoid guessing after the client has just rejected a proposed product."""
    wants_to_proceed = re.search(
        r"\b(lo quiero|me lo llevo|quiero comprar|quiero hacer el pedido|"
        r"(?:me )?gustar[ií]a proceder con la compra|quiero proceder con la compra)\b",
        message_text,
        flags=re.IGNORECASE,
    )
    if not wants_to_proceed:
        return False

    recent_customer_messages = [
        item.get("content", "")
        for item in prior_history[-4:]
        if item.get("role") == "user"
    ]
    if not recent_customer_messages:
        return False

    last_customer_message = recent_customer_messages[-1]
    return bool(
        re.search(
            r"\bno\b.{0,80}\b(quiero|interesa|avanzar|proceder|comprar)\b",
            last_customer_message,
            flags=re.IGNORECASE,
        )
    )


def _normalized_text(text: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(character) != "Mn"
    )


def _extract_quantity(text: str) -> int:
    """Extract an explicit purchase quantity, never a product measurement.

    A bare number inside ``8/8/10/12 mm`` is a lash length, not four units.
    A standalone number is accepted only because it is a natural answer to
    Fred's direct “¿cuántas unidades?” question.
    """
    normalized = _normalized_text(text).strip()
    patterns = (
        r"^\s*(\d{1,2})\s*$",
        # A natural correction ("mejor 3", "que sean 3", "cambialo a 2") is
        # just as explicit as the original number and must win over it.
        r"\b(?:mejor|mejor\s+que\s+sean|que\s+sean|sean|dejalo\s+en|cambia(?:lo)?\s+a|"
        r"pone(?:me|le)?|anota(?:me)?)\s+(\d{1,2})\b",
        r"\b(?:quiero|llevo|llevar|pido|pedir|ordenar|comprar|compro|necesito|serian|son|cantidad)\s+(\d{1,2})\b",
        r"\b(\d{1,2})\s*(?:x|unidades?|unidad|u|packs?|pares?)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            quantity = int(match.group(1))
            return quantity if 1 <= quantity <= 99 else 0
    return 0


def _is_sale_confirmation(text: str) -> bool:
    """Accept a normal WhatsApp confirmation, but not a new correction request."""
    normalized = _normalized_text(text).strip()
    return bool(
        re.fullmatch(
            r"(?:si(?:\s*[,\-]?\s*(?:confirmo|confimo|dale|ok|okay|listo))?|"
            r"confirmo|confimo|confirmar|dale|ok|okay|listo)",
            normalized,
        )
    )


def _extract_customer_details(text: str) -> tuple:
    email_match = re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", text)
    if not email_match:
        return ()

    name_text = text[:email_match.start()]
    # Prefer an explicit WhatsApp form label. It keeps a friendly message such
    # as "te dejo los datos" from becoming part of the customer's name.
    labeled_name = re.search(
        r"(?i)\b(?:nombre(?:\s+y\s+apellido)?|nombre completo)\s*:\s*"
        r"([^\r\n,;|]+?)(?=\s+(?:email|mail|correo)\s*:|[\r\n,;|]|$)",
        name_text,
    )
    if labeled_name:
        name_text = labeled_name.group(1)
    else:
        natural_name = re.search(
            r"(?i)\b(?:mi\s+)?nombre\s+(?:es|ser[ií]a)\s+"
            r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+){1,4}?)"
            r"(?=\s+(?:y\s+)?(?:mi\s+)?(?:email|mail|correo)\b|[,;\n]|$)",
            name_text,
        )
        if natural_name:
            name_text = natural_name.group(1)
        name_text = re.sub(
            r"(?i)^\s*(?:(?:genial|perfecto|dale|hola)\s*[,:;-]*\s*)?"
            r"(?:(?:te\s+)?(?:dejo|paso|mando|comparto)\s+)?"
            r"(?:(?:(?:los|mis)\s+)?(?:datos|detalles|informaci[oó]n|info)|todo)\s*[:,-]*\s*",
            "",
            name_text,
        )
    name_text = re.sub(r"(?i)\b(nombre|soy|mi mail|email|correo|es)\b\s*:? *", "", name_text)
    # Cuando la clienta responde el formato compacto ("envío, Ana Pérez,
    # ana@email.com"), logística no forma parte de su nombre.
    name_text = re.sub(r"(?i)\b(env[ií]o|retiro|retirar)\b\s*", "", name_text)
    name_text = re.sub(r"[,:;|]+", " ", name_text).strip()
    name_words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", name_text)
    if len(name_words) < 2:
        return ()
    return " ".join(name_words[:5]), email_match.group(0).lower()


def _extract_customer_fields(text: str) -> dict:
    """Extract only the identity fields explicitly present in a WhatsApp turn."""
    fields = {}
    email_match = re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", text)
    if email_match:
        fields["customer_email"] = email_match.group(0).lower()

    labeled_name = re.search(
        r"(?i)\b(?:nombre(?:\s+y\s+apellido)?|nombre completo)\s*:\s*"
        r"([^\r\n,;|]+?)(?=\s+(?:email|mail|correo)\s*:|[\r\n,;|]|$)",
        text,
    )
    natural_name = re.search(
        r"(?i)\b(?:mi\s+)?nombre\s+(?:es|ser[ií]a)\s+"
        r"([A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+){1,4})",
        text,
    )
    candidate = labeled_name.group(1) if labeled_name else (natural_name.group(1) if natural_name else "")
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", candidate)
    if len(words) >= 2:
        fields["customer_name"] = " ".join(words[:5])
    return fields


def _customer_details_prompt(include_quantity: bool = False) -> str:
    """Request checkout data in a small, copyable WhatsApp form."""
    quantity_line = "Cantidad: \n" if include_quantity else ""
    return (
        "¡Buenísimo! Para dejarlo listo, copiá y completá estas líneas. "
        "Así evitamos errores con el link 😊\n"
        "{}"
        "Nombre y apellido: \n"
        "Email: "
    ).format(quantity_line)


def _sales_fulfillment(text: str) -> str:
    normalized = _normalized_text(text)
    if any(word in normalized for word in ("retiro", "retirar", "showroom", "paso a buscar")):
        return "pickup"
    if any(word in normalized for word in ("envio", "enviar", "domicilio", "correo")):
        return "shipping"
    return ""


def _looks_like_new_customer_request(text: str) -> bool:
    """Do not trap a new question inside an old confirmation screen."""
    normalized = _normalized_text(text).strip()
    return bool(
        re.match(r"^(hola|buenas|buen dia|buenas tardes)\b", normalized)
        or re.search(
            r"\b(busco|quisiera saber|tienen|tenes|tendran|hay|me recomendas|"
            r"me gustaria comprar|quisiera comprar|quiero comprar|a que precio|cuanto sale|"
            r"mejor quiero|mejor prefiero|prefiero|mejor me quedo con|cambio de idea|"
            r"en vez de eso quiero)\b",
            normalized,
        )
    )


def _message_refers_to_intake_product(text: str, intake: dict) -> bool:
    """Return true when a new-looking message still names the active product."""
    normalized = _normalized_text(text)
    product = _normalized_text(str((intake or {}).get("product_request") or ""))
    ignored = {
        "shoow", "tools", "producto", "pestana", "pestanas", "pack", "color",
        "black", "chocolate", "unidades", "unidad",
    }
    distinctive = {
        token for token in re.findall(r"[a-z0-9]+", product)
        if len(token) >= 3 and token not in ignored
    }
    return bool(distinctive and any(token in normalized for token in distinctive))


def _looks_like_an_interruption_question(text: str) -> bool:
    """A genuine question unrelated to the checkout step Fred is waiting on
    -- e.g. "¿estas se pueden reutilizar?" while Fred is waiting for envío o
    retiro. Narrow on purpose: only a real interrogative, never a bare
    confirmation, number, or short answer to what was actually asked."""
    stripped = text.strip()
    if "?" in stripped or "¿" in stripped:
        return True
    normalized = _normalized_text(stripped)
    return bool(re.match(r"^(que|como|cuando|cuanto|cual|porque|para que|antes)\b", normalized))


def _simple_customer_reply(text: str) -> str:
    """Resolve social-only messages locally; no model or catalog lookup needed."""
    normalized = _normalized_text(text).strip()
    if re.fullmatch(r"(?:hola|holaa+|buenas|buen dia|buenas tardes|buenas noches|hello)", normalized):
        return "¡Hola! 😊 ¿En qué te puedo ayudar?"
    if re.fullmatch(r"(?:gracias|muchas gracias|genial gracias|perfecto gracias|ok gracias)", normalized):
        return "¡De nada! 😊 Si te surge otra duda, escribime por acá."
    return ""


def _lifting_clarification_reply(text: str) -> str:
    """Backward-compatible wrapper shared with the read-only shadow."""
    return lifting_clarification_reply(text)


def _customer_access_reply(customer_phone: str) -> str:
    """Return a no-AI reply when Fred is deliberately limited or paused."""
    normalized_phone = re.sub(r"\D", "", customer_phone)
    if FRED_CUSTOMER_MODE == "paused":
        return "Estamos haciendo un ajuste breve en la atención por acá. Probá de nuevo en unos minutos 😊"
    if FRED_CUSTOMER_MODE == "allowlist" and normalized_phone not in FRED_BETA_ALLOWED_PHONES:
        return "Estamos terminando de habilitar la atención por este número. En breve te respondemos por acá 😊"
    return ""


def _send_service_fallback(
    customer_phone: str,
    conversation_id: int,
    message_text: str,
    prior_history: list,
    summary: str,
) -> None:
    """Fail safely: do not invent a commercial answer when a dependency fails.

    A technical outage no longer opens a case for Isa -- the customer is told
    the truth and given her contact so they are never left waiting on a queue
    nobody is watching.
    """
    reply = _isa_direct_contact_reply(
        "Uy, ahora no puedo consultar esto bien y prefiero no darte un dato incorrecto."
    )
    if send_whatsapp_text(customer_phone, reply) and conversation_id:
        try:
            record_bot_message(conversation_id, reply)
        except Exception as error:  # noqa: BLE001
            print("ERROR guardando respuesta de contingencia (tipo: {}).".format(type(error).__name__))
    print("[Fred] Fallback de servicio entregado ({}).".format(summary))


def _sales_summary(intake: dict) -> str:
    fulfillment = "envío" if intake["fulfillment"] == "shipping" else "retiro"
    price_summary = ""
    if intake["unit_price"] is not None:
        try:
            subtotal = Decimal(str(intake["unit_price"])) * intake["quantity"]
            formatted_subtotal = _format_ars(subtotal)
            price_summary = (
                "Subtotal de productos: {}\n"
                "Envío: a confirmar\n"
                "Total final: a confirmar\n"
            ).format(formatted_subtotal)
        except (InvalidOperation, TypeError):
            pass

    return (
        "Te resumo antes de pasárselo a Isa:\n"
        "Producto/modelo: {}\n"
        "Variante: {}\n"
        "Cantidad: {}\n"
        "Entrega: {}\n"
        "Nombre: {}\n"
        "Email: {}\n\n"
        "{}\n"
        "¿Confirmás que lo preparemos para revisión?"
    ).format(
        intake["product_request"],
        intake["selected_variant"] or "a confirmar",
        intake["quantity"],
        fulfillment,
        intake["customer_name"],
        intake["customer_email"],
        price_summary,
    )


def _recent_candidate_quantity(prior_history: list, sale_candidate: dict) -> int:
    product_words = {
        word for word in _normalized_text(sale_candidate.get("product_name", "")).split()
        if len(word) >= 4
    }
    for item in reversed(prior_history[-6:]):
        if item.get("role") != "user":
            continue
        message = item.get("content", "")
        if product_words.intersection(_normalized_text(message).split()):
            quantity = _extract_quantity(message)
            if quantity:
                return quantity
    return 0


def _record_agent_turn_safely(
    *,
    wa_message_id: str,
    conversation_id: int,
    result: dict,
    action: str,
    reason: str,
    outcome: str,
    catalog_context_used: bool,
    knowledge_context_used: bool,
    duration_ms: int,
) -> None:
    """Telemetry must never block an answer or expose a storage error."""
    usage = result.get("usage") or {}
    try:
        record_agent_turn(
            source_message_id=wa_message_id or "",
            conversation_id=conversation_id,
            action=action,
            reason=reason or "unknown",
            outcome=outcome,
            tool_names=[
                call.get("name", "") for call in result.get("tool_calls", [])
            ],
            catalog_context_used=catalog_context_used,
            knowledge_context_used=knowledge_context_used,
            model_calls=result.get("model_calls", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            duration_ms=duration_ms,
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR guardando observabilidad (tipo: {})".format(type(error).__name__))


def _verified_purchase_candidate_from_tool_calls(message_text: str, result: dict) -> dict:
    """Recover a concrete sale choice when the model verified one SKU but omitted the marker.

    The language model is allowed to explain the product, but it must not be the
    source of truth for the sales-form state. When a customer explicitly names
    a purchase and the same turn checked exactly one SKU, re-check that SKU and
    start the deterministic intake form. More than one stock lookup means the
    choice is ambiguous, so we intentionally do nothing here.
    """
    normalized = _normalized_text(message_text)
    expresses_purchase = bool(
        re.search(
            r"\b(comprar|compra|pedir|pido|ordenar|llevar|llevo|avanzar|avancemos|proceder)\b",
            normalized,
        )
    )
    if not expresses_purchase:
        return {}

    checked_skus = [
        (call.get("arguments", {}).get("sku") or "").strip()
        for call in result.get("tool_calls", [])
        if call.get("name") == "get_stock"
    ]
    checked_skus = list(dict.fromkeys(sku for sku in checked_skus if sku))
    if len(checked_skus) != 1:
        return {}

    stock = get_stock(checked_skus[0])
    if stock.get("status") != "in_stock":
        return {}

    return {
        "sku": stock["sku"],
        "product_name": stock["product_name"],
        "variant": stock.get("variant") or "",
        "unit_price": stock.get("price"),
    }


def _already_asked_product_clarification(prior_history: list) -> bool:
    """Return True only after Fred already asked once to identify a model."""
    for item in reversed(prior_history[-4:]):
        if item.get("role") != "assistant":
            continue
        text = _normalized_text(item.get("content", ""))
        return "asegurarme de ubicar el modelo correcto" in text
    return False


def _start_sales_intake(
    conversation_id: int,
    sale_candidate: dict = None,
    quantity: int = 0,
) -> str:
    if sale_candidate:
        product_request = sale_candidate["product_name"]
        selected_variant = sale_candidate.get("variant") or ""
        start_sales_intake(
            conversation_id,
            product_request=product_request,
            selected_sku=sale_candidate["sku"],
            selected_variant=selected_variant,
            unit_price=sale_candidate.get("unit_price"),
            quantity=quantity or None,
        )
        # Ask in words for everything still missing instead of forcing the
        # two-button screen: the customer can answer in one natural line.
        return _sales_missing_step({
            "quantity": quantity or None, "fulfillment": None,
            "customer_name": None, "customer_email": None,
        })

    start_sales_intake(conversation_id)
    return (
        "¡Dale! Para prepararte el link necesito confirmar bien el producto. "
        "¿Qué modelo o variante querés llevar?"
    )


def _apply_sale_details_from_same_message(conversation_id: int, message_text: str) -> str:
    """Keep checkout details the customer already gave in their purchase message.

    A client may naturally write the product, quantity, delivery preference,
    name and email in one WhatsApp message.  Once the product is verified, we
    must not make them repeat those details just because the persisted sales
    form was opened a few lines later in the code.
    """
    intake = get_active_sales_intake(conversation_id)
    if not intake or not intake.get("quantity"):
        return ""

    _apply_sale_turn_updates(conversation_id, message_text, intake)
    refreshed = get_active_sales_intake(conversation_id)
    return _sales_summary(refreshed) if _sale_is_complete(refreshed) else ""


def _sale_is_complete(intake: dict) -> bool:
    return bool(
        intake
        and intake.get("quantity")
        and intake.get("fulfillment")
        and intake.get("customer_name")
        and intake.get("customer_email")
    )


def _apply_sale_turn_updates(conversation_id: int, message_text: str, intake: dict = None) -> dict:
    """Save every explicit field in a natural customer message.

    The latest explicit value wins, so “perdón, son 3” replaces a prior two
    even if Fred was asking for another field at that moment.
    """
    intake = intake or get_active_sales_intake(conversation_id)
    if not intake:
        return {}
    values = {}
    quantity = _extract_quantity(message_text)
    fulfillment = _sales_fulfillment(message_text)
    details = _extract_customer_details(message_text)
    fields = _extract_customer_fields(message_text)
    if quantity:
        values["quantity"] = quantity
    if fulfillment:
        values["fulfillment"] = fulfillment
    if details:
        values["customer_name"], values["customer_email"] = details
    values.update(fields)
    # Do not rewrite a field merely because the client repeated it. Apart from
    # avoiding needless writes, this keeps a stale form step from firing twice.
    values = {field: value for field, value in values.items() if intake.get(field) != value}
    if not values:
        return intake

    merged = dict(intake)
    merged.update(values)
    next_status = (
        "quantity" if not merged.get("quantity") else
        "fulfillment" if not merged.get("fulfillment") else
        "customer" if not (merged.get("customer_name") and merged.get("customer_email")) else
        "confirmation"
    )
    complete_identity = "customer_name" in values and "customer_email" in values
    if "quantity" in values:
        set_sales_intake_quantity(conversation_id, values.pop("quantity"))
    if "fulfillment" in values:
        set_sales_intake_fulfillment(conversation_id, values.pop("fulfillment"))
    if complete_identity:
        set_sales_intake_customer(
            conversation_id,
            values.pop("customer_name"),
            values.pop("customer_email"),
        )
    # The small legacy setters move through intermediate statuses (quantity ->
    # fulfillment -> customer). A correction to an otherwise complete order
    # must finish in confirmation again; otherwise “sí, confirmo” would ask
    # for contact data a second time.
    update_sales_intake_fields(conversation_id, next_status, **values)
    return merged


def _sales_missing_step(intake: dict) -> str:
    """Ask for everything that's actually missing in ONE natural message.

    A checkout is a draft of known facts, not a wizard: making someone answer
    four separate questions when they could have written one line is friction
    we impose, not information we need. The customer can still answer them one
    at a time -- each message fills in whatever it carries.
    """
    missing = []
    if not intake.get("quantity"):
        missing.append("cuántas unidades querés")
    if not intake.get("fulfillment"):
        missing.append("si preferís envío o retiro")
    if not intake.get("customer_name"):
        missing.append("tu nombre y apellido")
    if not intake.get("customer_email"):
        missing.append("tu email")

    if not missing:
        return ""
    if missing == ["si preferís envío o retiro"]:
        # Buttons stay available as a shortcut, never as a requirement: Fred
        # reads "envío"/"retiro" written in words just as well, so the reply
        # is worded as a question either way.
        return "__FULFILLMENT_BUTTONS__" if FULFILLMENT_BUTTONS_ENABLED else (
            "Me falta únicamente saber si preferís envío o retiro 😊"
        )
    if len(missing) == 1:
        return "Me falta únicamente {} y lo dejamos listo 😊".format(missing[0])
    joined = "{} y {}".format(", ".join(missing[:-1]), missing[-1])
    return (
        "Para dejar la compra lista me falta {}. Podés mandarme todo junto si querés 😊"
    ).format(joined)


def _handle_sales_intake(
    conversation_id: int,
    customer_phone: str,
    message_text: str,
    intake: dict,
    prior_history: list,
) -> Optional[bool]:
    """Run the one purchase flow used by every normal checkout.

    A sale is a record of known facts, not a chain of fragile screens.  Each
    customer message may add or correct any field; Fred then asks only for the
    next missing fact, or sends the completed record to Isa after confirmation.

    Returns True when handled normally, False when the checkout was released
    because the message is about a different product, or None when the
    message is a genuine question unrelated to the pending field -- in that
    case the intake is left completely untouched so CHAT can answer and the
    checkout resumes exactly where it was on the next message.
    """
    normalized = _normalized_text(message_text)
    # Numeric aliases (1=confirmar, 2=modificar, 3=cancelar, 4=hablar con Isa)
    # only mean this once the summary has actually been shown -- during
    # "quantity" a bare "2" is 2 unidades, not a menu selection.
    if intake.get("status") == "confirmation" and normalized in {"1", "2", "3", "4"}:
        normalized = {"1": "confirmo", "2": "cambiar", "3": "cancelar", "4": "hablar con isa"}[normalized]
        message_text = normalized
    if re.fullmatch(r"(?:cancelar|cancelo|dejalo|no sigo)", normalized):
        cancel_sales_intake(conversation_id)
        reply = "Dale, cancelé esta preparación. Si querés volver a empezar, avisame 😊"
    elif normalized == "hablar con isa" and intake.get("status") == "confirmation":
        reply = _isa_direct_contact_reply("Dale, lo vemos con Isa.")
    elif (
        _looks_like_new_customer_request(message_text)
        and not _message_refers_to_intake_product(message_text, intake)
    ):
        # Discovery of a different product outranks an unfinished checkout.
        # Release both pieces of old state, then let the normal catalog/RAG
        # path handle this same message instead of asking checkout fields.
        cancel_sales_intake(conversation_id)
        clear_product_selection(conversation_id)
        return False
    elif intake["status"] == "product" and not intake.get("selected_sku"):
        # This fallback only applies when Fred truly has no verified product
        # yet. Normal checkout always starts with a verified SKU above.
        set_sales_intake_product(conversation_id, message_text)
        reply = "Perfecto. ¿Cuántas unidades querés?"
    else:
        updated_intake = _apply_sale_turn_updates(conversation_id, message_text, intake)
        changed = updated_intake != intake

        if (
            not changed
            and not _sale_is_complete(updated_intake)
            and _looks_like_an_interruption_question(message_text)
        ):
            # A real question, not an attempt to answer the missing field --
            # e.g. "¿son reutilizables?" while Fred is waiting for envío o
            # retiro. Nothing about the checkout changes; CHAT answers this
            # one message and the flow resumes on the next.
            return None
        integrity_error = (
            _purchase_draft_integrity_error(updated_intake)
            if _sale_is_complete(updated_intake) else ""
        )
        if integrity_error:
            # Never present or escalate a purchase whose identity we cannot
            # stand behind. Better to reopen the product question than to show
            # a confident summary for the wrong thing.
            print("[Checkout] Borrador bloqueado por integridad: {}.".format(integrity_error))
            cancel_sales_intake(conversation_id)
            reset_fred_core_checkout(conversation_id)
            reply = (
                "Antes de pasarlo necesito confirmar bien el producto: {}. "
                "¿Me decís de nuevo cuál querés llevar y lo verifico en el momento? 😊"
            ).format(integrity_error)
        elif not _sale_is_complete(updated_intake):
            reply = _sales_missing_step(updated_intake)
        elif _reads_as_negation(message_text):
            # "no", "mejor no", "cancelalo" frente al resumen ya mostrado.
            cancel_sales_intake(conversation_id)
            reply = "Dale, lo dejamos acá. Si querés retomarlo más adelante, avisame 😊"
        elif _is_sale_confirmation(message_text) or _reads_as_affirmation(message_text):
            mark_sales_intake_ready(conversation_id)
            sale_draft = {
                "status": "ready_for_isa_review",
                "items_status": "{} × {}".format(
                    updated_intake["quantity"], updated_intake["product_request"]
                ),
                "selected_sku": updated_intake["selected_sku"] or "a confirmar",
                "selected_variant": updated_intake["selected_variant"] or "a confirmar",
                "unit_price": str(updated_intake["unit_price"]) if updated_intake["unit_price"] is not None else "a confirmar",
                "products_subtotal": (
                    str(Decimal(str(updated_intake["unit_price"])) * updated_intake["quantity"])
                    if updated_intake["unit_price"] is not None
                    else "a confirmar"
                ),
                "delivery_status": "envío" if updated_intake["fulfillment"] == "shipping" else "retiro",
                "payment_status": "link pendiente de aprobación de Isa",
                "customer_name": updated_intake["customer_name"],
                "customer_email": updated_intake["customer_email"],
                "order_creation": "disabled until Isa approval",
            }
            notified = _queue_for_isa(
                conversation_id,
                customer_phone,
                "purchase_review",
                "La clienta confirmó una ficha de venta completa.",
                message_text,
                conversation_context=prior_history,
                sale_draft=sale_draft,
            )
            if notified:
                reply = (
                    "Perfecto, ya se lo pasé a Isa para que revise los detalles antes "
                    "de generar cualquier link 😊"
                )
            else:
                reply = (
                    "Perfecto, tu solicitud quedó registrada para que Isa la revise antes "
                    "de generar cualquier link. Apenas la apruebe te mando el link por acá 😊"
                )
        elif re.match(r"^(?:quiero\s+)?(?:cambiar|corregir)(?:lo)?\b", normalized):
            reply = "Claro 😊 Decime qué querés cambiar y actualizo el resumen."
        elif (
            changed
            or _sales_fulfillment(message_text)
            or _extract_customer_fields(message_text)
            or _extract_customer_details(message_text)
        ):
            reply = _sales_summary(updated_intake)
        else:
            reply = "¿Confirmás el resumen? Respondé “confirmo” o decime si querés corregirlo."
        if reply is not None and "¿Confirmás" in reply:
            # Fred está por preguntar algo cuya respuesta ejecuta una acción:
            # persistir la intención ANTES de enviarla, para que un "sí" o un
            # "dale" del próximo turno tenga a qué referirse.
            _remember_pending_intent(conversation_id, PENDING_CONFIRM_PURCHASE_DRAFT)

    if reply == "__FULFILLMENT_BUTTONS__":
        if send_customer_fulfillment_buttons(customer_phone):
            record_bot_message(conversation_id, "¿Cómo preferís recibir tu compra? [Envío / Retiro]")
    elif send_whatsapp_text(customer_phone, reply):
        record_bot_message(conversation_id, reply)
    return True


def _isa_feedback_text(message_text: str) -> str:
    """Extract explicit internal feedback without treating normal messages as feedback."""
    match = re.match(r"^\s*feedback\s*:\s*(.+)$", message_text, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _handle_isa_reminder_request(message_text: str) -> bool:
    """Let Isa manage reminders in ordinary language, without a separate panel."""
    normalized = _normalized_text(message_text)
    now = datetime.now(ARGENTINA_TZ)

    if re.search(r"\b(no me recuerdes|silencia|silencia los recordatorios)\b", normalized):
        tomorrow = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        snooze_isa_reminders(ISA_WHATSAPP_NUMBER, tomorrow)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Dale, no te insisto más hoy. Si sigue pendiente, te lo recuerdo mañana a las 10 😊",
        )
        return True

    match = re.search(r"\brecordame en (\d{1,2})\s*(minuto|min|hora|horas|h)\b", normalized)
    if match:
        amount = int(match.group(1))
        minutes = amount if match.group(2).startswith("min") else amount * 60
        if not 10 <= minutes <= 12 * 60:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Puedo recordártelo entre 10 minutos y 12 horas. Por ejemplo: “recordame en 1 hora”.",
            )
            return True
        remind_at = now + timedelta(minutes=minutes)
        snooze_isa_reminders(ISA_WHATSAPP_NUMBER, remind_at)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Dale, te lo recuerdo a las {}. Mientras tanto no te molesto 😊".format(
                remind_at.strftime("%H:%M")
            ),
        )
        return True

    if re.search(r"\b(reactiva|volve a recordar|vuelve a recordar|recordame ahora)\b", normalized):
        clear_isa_reminder_snooze(ISA_WHATSAPP_NUMBER)
        snapshot = pending_reminder_snapshot()
        if snapshot["count"]:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Listo, vuelvo a avisarte. Tenés {} pendiente(s); escribime “ver” y te muestro el primero.".format(
                    snapshot["count"]
                ),
            )
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, no hay pendientes ahora 😊")
        return True

    return False


def _handle_isa_operations_summary_request(message_text: str) -> bool:
    """Give Isa an on-demand factual summary without waiting for 21:00."""
    normalized = _normalized_text(message_text)
    if not re.fullmatch(r"(?:resumen|resumen de hoy|como vamos|como va fred|estado)", normalized):
        return False
    try:
        summary = daily_operations_summary()
    except Exception as error:  # noqa: BLE001
        print("ERROR armando resumen operativo (tipo: {}).".format(type(error).__name__))
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No pude armar el resumen ahora. Probá de nuevo en unos minutos.")
        return True
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Resumen de Fred hoy 😊\n"
        "• Conversaciones atendidas: {conversations}\n"
        "• Checkouts aprobados: {approved_checkouts}\n"
        "• Pagos confirmados por Tiendanube: {paid_orders}\n"
        "• Pendientes para revisar: {pending}\n\n"
        "Para ver los chats completos, entrá al panel privado de Fred.".format(**summary),
    )
    return True


def _handle_isa_quality_review_request(message_text: str) -> bool:
    """Give Isa an on-demand quality snapshot; it is observation, not an alert."""
    normalized = _normalized_text(message_text).strip()
    if not re.fullmatch(
        r"(?:calidad|revisar calidad|control de calidad|como estuvo fred|que mejorar)",
        normalized,
    ):
        return False
    try:
        snapshot = daily_quality_snapshot()
    except Exception as error:  # noqa: BLE001
        print("ERROR armando control de calidad (tipo: {}).".format(type(error).__name__))
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No pude armar el control ahora. Probá de nuevo en unos minutos.")
        return True

    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Control de calidad de Fred hoy 😊\n"
        "• Pendientes abiertos: {pending_actions}\n"
        "• Casos donde Fred pidió ayuda: {bot_fallbacks_today}\n"
        "• Clientas que pidieron hablar con Isa: {human_handoffs_today}\n"
        "• Encargos / preventas / mayoristas: {special_sales_today}\n"
        "• Compras esperando aprobación: {pending_purchase_reviews}\n\n"
        "No todo caso escalado es un error: son los chats que más valor tiene revisar. "
        "Para abrirlos, entrá al panel o escribí “ver”.".format(**snapshot),
    )
    return True


def _isa_demo_order_request(message_text: str) -> tuple:
    """Parse Isa's explicit demo-only order command."""
    match = re.match(
        r"^\s*demo\s*:\s*([A-Za-z0-9_-]+)\s*[x×]\s*(\d+)\s*$",
        message_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ()
    return match.group(1), int(match.group(2))


def _is_demo_command(message_text: str) -> bool:
    return bool(re.match(r"^\s*demo\b", message_text, flags=re.IGNORECASE))


ISA_SALE_TYPE_LABELS = {
    "normal": "Venta normal",
    "encargo": "Encargo",
    "venta_mayorista": "Venta mayorista",
    "otro": "Otro",
}


def _looks_like_isa_sale_request(message_text: str) -> bool:
    """Understand natural internal requests without requiring a magic command."""
    normalized = _normalized_text(message_text)
    return bool(
        re.search(
            r"\b(vendi|venta|orden|link de pago|link|cobrar|registrar|pedido)\b",
            normalized,
        )
    )


def send_isa_sale_type_menu() -> bool:
    """Ask Isa to classify an external sale with a WhatsApp list, not syntax."""
    url = f"https://graph.facebook.com/v26.0/{WHATSAPP_PHONE_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": normalize_whatsapp_recipient(ISA_WHATSAPP_NUMBER),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": (
                    "¿Cómo querés registrar esta venta? Elegí una opción y después "
                    "te pido los datos. Todavía no se crea ningún link."
                )
            },
            "action": {
                "button": "Elegir tipo",
                "sections": [
                    {
                        "title": "Tipo de venta",
                        "rows": [
                            {"id": "sale_type:normal", "title": "Venta normal", "description": "Producto con stock físico"},
                            {"id": "sale_type:encargo", "title": "Encargo", "description": "Producto a pedir / sin stock físico"},
                            {"id": "sale_type:venta_mayorista", "title": "Venta mayorista", "description": "Condición comercial especial"},
                            {"id": "sale_type:otro", "title": "Otro", "description": "Contame el caso y lo clasificamos"},
                        ],
                    }
                ],
            },
        },
    }
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        print(f"[Isa] Menú interno HTTP {response.status_code}")
        response.raise_for_status()
        return True
    except Exception as error:  # noqa: BLE001
        print(f"ERROR enviando menú interno a Isa: {type(error).__name__}")
        return False


def _isa_sale_type_prompt(sale_type: str) -> str:
    if sale_type == "otro":
        return (
            "Listo, marcamos ‘Otro’. Contame brevemente qué pasó y qué necesitás. "
            "No voy a crear nada hasta que el tipo de venta quede claro."
        )
    return (
        "Perfecto: {}. Ahora pasame en un solo mensaje producto, variante, cantidad "
        "y nombre/email de la clienta si lo tenés. Voy a armar un borrador para tu "
        "aprobación; todavía no se crea ningún link."
    ).format(ISA_SALE_TYPE_LABELS[sale_type])


def _handle_isa_sale_session(message_text: str, button_reply_id: str) -> bool:
    """Advance Isa's internal guided draft. Returns True when it handled input."""
    if button_reply_id.startswith("sale_type:"):
        sale_type = button_reply_id.split(":", 1)[1]
        if sale_type not in ISA_SALE_TYPE_LABELS:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No reconocí ese tipo. Elegí una opción de la lista.")
            return True
        set_isa_sale_session_type(ISA_WHATSAPP_NUMBER, sale_type)
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, _isa_sale_type_prompt(sale_type))
        return True

    # Approval/context buttons belong to customer pending actions, never to an
    # unfinished internal sale draft.
    if button_reply_id:
        return False

    session = get_isa_sale_session(ISA_WHATSAPP_NUMBER)
    if not session:
        return False

    normalized = _normalized_text(message_text).strip()
    if re.fullmatch(r"(?:cancelar borrador|descartar borrador|cancelar venta)", normalized):
        clear_isa_sale_session(ISA_WHATSAPP_NUMBER)
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, descarté ese borrador interno. No se creó nada.")
        return True

    # "Cancelar" used to silently discard Isa's own draft even when the visible
    # thing she meant was a customer's pending approval. Keep those actions
    # deliberately separate: only the card button can return a customer to Fred.
    if re.fullmatch(r"(?:cancelar|cancelalo|dejalo)", normalized):
        if pending_action_count():
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Veo una clienta pendiente. Para devolver ese chat a Fred, tocá “Descartar” "
                "en su tarjeta. Si querías cerrar solo tu borrador interno, escribí “cancelar borrador”.",
            )
        else:
            clear_isa_sale_session(ISA_WHATSAPP_NUMBER)
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, descarté ese borrador interno. No se creó nada.")
        return True

    if session["status"] == "choose_type":
        send_isa_sale_type_menu()
        return True

    if session["status"] == "collect_details":
        add_isa_sale_session_details(ISA_WHATSAPP_NUMBER, message_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Borrador de {} guardado ✅\n\n{}\n\n"
            "Todavía no se creó ninguna orden ni link. La próxima fase agrega la "
            "revisión y tu botón de aprobación."
            .format(ISA_SALE_TYPE_LABELS[session["sale_type"]], message_text[:600]),
        )
        return True

    if session["status"] == "review":
        if _looks_like_isa_sale_request(message_text):
            start_isa_sale_session(ISA_WHATSAPP_NUMBER)
            send_isa_sale_type_menu()
            return True
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Ese borrador ya está guardado. Si querés cerrarlo, escribí “cancelar borrador”; "
            "o mandame una nueva venta para empezar otra ficha.",
        )
        return True

    return False


def _pending_action_by_id(action_id: int) -> dict:
    """Read one still-pending card; used before a demo-only side effect."""
    return next(
        (item for item in list_pending_actions(limit=20) if item["id"] == action_id),
        None,
    )


def _send_special_sale_conditions(action: dict) -> None:
    """Send the owner-approved encargo PDF and a short, clear next step."""
    if action.get("action_type") != "special_sale_request":
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente no corresponde a un encargo.")
        return

    if not send_whatsapp_document(
        action["customer_phone"],
        ENCARGOS_PDF_URL,
        "Beauty-House-Preventa-y-Encargos.pdf",
        "Te compartimos las condiciones vigentes para preventas y encargos.",
    ):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "No pude enviar el PDF a la clienta. El pendiente sigue abierto; probá de nuevo.",
        )
        return

    customer_text = (
        "¡Gracias por consultarnos! 😊 Te envié el PDF con las condiciones vigentes "
        "para preventas y encargos. Isa revisa la disponibilidad y te confirma la "
        "cotización final antes de generar cualquier link o reserva."
    )
    if not send_whatsapp_text(action["customer_phone"], customer_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "El PDF llegó a la clienta, pero no pude enviarle el mensaje de seguimiento. El pendiente sigue abierto.",
        )
        return

    result = resolve_pending_action(action["id"], "approved")
    if not result:
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Las condiciones llegaron a la clienta, pero el pendiente cambió de estado. Revisalo antes de seguir.",
        )
        return
    set_conversation_state(result["conversation_id"], "BOT")
    record_bot_message(result["conversation_id"], customer_text)
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Listo, Fred le envió el PDF vigente de preventas y encargos. Si querés darle una respuesta puntual, usá “Responder a Fred”.",
    )
    if pending_action_count():
        send_next_pending_to_isa()


def _is_special_conditions_request(message_text: str) -> bool:
    normalized = _normalized_text(message_text)
    return bool(
        re.search(r"\b(condiciones?|informacion)\b", normalized)
        and re.search(r"\b(encargo|encargos|preventa|cotizacion)\b", normalized)
    )


def _isa_customer_instruction(message_text: str, action: dict) -> str:
    """Translate a few safe, natural owner instructions into client content.

    This prevents commands such as "mandar políticas en pdf" from being sent
    literally to a customer.  It is intentionally tiny: all other free-form
    text remains Isa's reviewed wording.
    """
    normalized = _normalized_text(message_text)
    is_send = bool(re.search(r"\b(manda|mandar|envia|enviar|pasa|pasar|comparti|compartir)\b", normalized))
    if not is_send:
        return ""

    if re.search(r"\b(politica|politicas|devolucion|devoluciones|cambio|cambios)\b", normalized):
        return (
            "Te comparto las políticas vigentes para que las puedas revisar: "
            "{}\n\nSi querés, contame brevemente qué pasó con el producto y "
            "te ayudo a dejar la consulta bien encaminada 😊"
        ).format(CUSTOMER_POLICIES_URL)

    if action.get("action_type") == "special_sale_request" and re.search(
        r"\b(condicion|condiciones|encargo|preventa|cotizacion)\b", normalized
    ):
        return "__SEND_SPECIAL_CONDITIONS__"

    return ""


ISA_OPTIONS_LEGEND = (
    "Te cuento qué hace cada opción 😊\n\n"
    "EN UNA COMPRA PARA REVISAR:\n"
    "• Aprobar compra: reviso stock y precio en vivo otra vez y, si sigue todo bien, "
    "genero el link de pago y se lo mando a la clienta. Recién ahí se crea el checkout.\n"
    "• Rechazar: te pido el motivo en un mensaje y se lo explico a la clienta con mis "
    "palabras. No se genera ningún link.\n"
    "• Pedir algo: me escribís qué necesitás preguntarle y yo se lo pregunto. La compra "
    "queda abierta esperando su respuesta, y te aviso apenas conteste.\n\n"
    "EN UNA CONSULTA:\n"
    "• Responder y cerrar: me escribís la respuesta, se la paso, y después yo sigo "
    "atendiendo normalmente.\n"
    "• Seguir conversación: TOMÁS VOS EL CHAT. Desde ese momento todo lo que escriba "
    "la clienta te llega a vos y yo no respondo nada, aunque sepa la respuesta. Lo que "
    "me escribas se lo mando tal cual, con tus palabras.\n"
    "• Devolver a Fred: termina tu asesoramiento y vuelvo a atender yo. También podés "
    "escribirme “devolver a Fred”.\n"
    "• Cerrar consulta: doy el caso por terminado.\n\n"
    "En cualquier momento podés escribirme “ver” para ver el próximo pendiente."
)

# Deliberately precise. This runs before every handler that could act on a
# case, so a false positive would swallow a real rejection reason or a real
# answer to the customer -- "no hay opciones disponibles" must NOT read as a
# request for help. Bare help words only count at the start of the message.
_ISA_HELP_RE = re.compile(
    r"^(?:ayuda|help|dudas)\b"
    r"|\bque\s+(?:hace|significa|significan)\s+cada\b"
    r"|\bpara\s+que\s+sirve\s+cada\b"
    r"|\bque\s+hago\s+con\s+cada\b"
    r"|\b(?:explicame|explicarme|no\s+entiendo)\s+(?:bien\s+)?(?:las\s+)?opciones\b"
    r"|\bque\s+pasa\s+si\s+(?:le\s+doy\s+a\s+|toco\s+|elijo\s+|aprieto\s+)?"
    r"(?:apruebo|aprueba|aprobar|rechazo|rechaza|rechazar|pido|pedir|"
    r"cierro|cerrar|respondo|responder|elijo|elegir|toco|sigo|seguir)\b"
)


def _isa_asks_for_legend(message_text: str) -> bool:
    """Isa asking what the options do -- answered with the legend instead of
    being treated as an answer to whatever case is open. Explaining never
    modifies the case: the caller returns right after sending the legend."""
    return bool(_ISA_HELP_RE.search(_normalized_text(message_text).strip()))


ISA_OWNS_KIND = "isa_owns"


def _isa_owned_case() -> Optional[dict]:
    """The one open case where Isa took the thread, if any."""
    try:
        return next(
            (
                item for item in list_pending_actions(limit=20)
                if item.get("payload", {}).get("awaiting_isa_kind") == ISA_OWNS_KIND
            ),
            None,
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR buscando conversación tomada por Isa (tipo: {}).".format(type(error).__name__))
        return None


def _relay_customer_message_to_isa(
    conversation_id: int, customer_phone: str, message_text: str,
) -> None:
    """While Isa owns the thread, the customer talks to her through Fred."""
    if not send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "💬 {}:\n\n{}".format(customer_phone, message_text.strip()[:900]),
    ):
        print("[Isa] No pude reenviar el mensaje de la clienta.")
        return
    print("[Isa] Mensaje de la clienta reenviado (conversación tomada por Isa).")


def _hand_thread_back_to_fred(action: dict, notify_customer: bool = True) -> None:
    """End Isa's human session and let Fred answer normally again."""
    conversation_id = action.get("conversation_id")
    try:
        clear_isa_awaiting(action["id"])
        resolve_pending_action(action["id"], "approved")
    except Exception as error:  # noqa: BLE001
        print("ERROR cerrando la sesión de Isa (tipo: {}).".format(type(error).__name__))
    if conversation_id:
        set_conversation_state(conversation_id, "BOT")
    if notify_customer and action.get("customer_phone"):
        closing = "Seguimos por acá 😊 Cualquier otra cosa, contame."
        if send_whatsapp_text(action["customer_phone"], closing) and conversation_id:
            record_bot_message(conversation_id, closing)
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Listo, le devolví el chat a Fred. Vuelve a atender normalmente 😊",
    )


def _forward_customer_answer_to_isa(
    conversation_id: int, customer_phone: str, message_text: str,
) -> None:
    """Relay this customer's message to Isa when it answers something she
    asked through Fred on a still-open case.

    Deliberately narrow (see the "an open case must not block Fred" rule): it
    only fires for the one case that is explicitly waiting on THIS customer,
    and it never stops Fred from also answering the message normally.
    """
    try:
        waiting = next(
            (
                item for item in list_pending_actions(limit=20)
                if item.get("conversation_id") == conversation_id
                and item.get("payload", {}).get("awaiting_isa_kind") == "customer_answer"
            ),
            None,
        )
    except Exception as error:  # noqa: BLE001
        print("ERROR revisando casos abiertos de Isa (tipo: {}).".format(type(error).__name__))
        return
    if not waiting:
        return
    label = "compra en revisión" if waiting["action_type"] == "purchase_review" else "consulta"
    delivered = send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Respuesta de la clienta ({} #{}):\n\n“{}”\n\n"
        "Escribime “ver” para retomar ese pendiente y decidir.".format(
            label, waiting["id"], message_text.strip()[:600],
        ),
    )
    if delivered:
        try:
            clear_isa_awaiting(waiting["id"])
        except Exception as error:  # noqa: BLE001
            print("ERROR limpiando espera de la clienta (tipo: {}).".format(type(error).__name__))
        print("[Isa] Respuesta de la clienta reenviada al pendiente #{}.".format(waiting["id"]))


def _reject_purchase_with_reason(action: dict, reason: str) -> None:
    """Isa declined a purchase and said why. The customer gets that reason in
    plain language plus a real way forward -- never a bare "no se pudo" -- and
    no checkout is ever created."""
    reason = _reason_for_customer(reason)
    customer_text = (
        "Isa revisó tu compra y por ahora no podemos avanzar.\n\n{}\n\n"
        "¿Querés que busquemos otra cantidad u otra opción, o preferís que le "
        "consulte algo puntual a Isa? 😊"
    ).format(reason) if reason else (
        "Isa revisó tu compra y por ahora no podemos avanzar con el pedido tal "
        "como estaba. ¿Querés que busquemos otra cantidad u otra opción? 😊"
    )
    if not send_whatsapp_text(action["customer_phone"], customer_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "No pude avisarle a la clienta; el pendiente sigue abierto. Probá de nuevo.",
        )
        return
    result = resolve_pending_action(action["id"], "rejected")
    conversation_id = action.get("conversation_id") or (result or {}).get("conversation_id")
    if conversation_id:
        record_bot_message(conversation_id, customer_text)
        set_conversation_state(conversation_id, "BOT")
        try:
            # The purchase is off, but the product and the conversation stay:
            # she may well want a different quantity of the same thing.
            cancel_sales_intake(conversation_id)
            reset_fred_core_checkout(conversation_id)
        except Exception as error:  # noqa: BLE001
            print("ERROR limpiando checkout rechazado (tipo: {}).".format(type(error).__name__))
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Listo, le expliqué el motivo y no generé ningún link. Fred sigue atendiéndola 😊",
    )
    if pending_action_count():
        send_next_pending_to_isa()


def _ask_customer_for_purchase(action: dict, question: str) -> None:
    """Isa needs something from the customer before deciding. The purchase
    review stays OPEN so her answer comes back to this same case instead of
    restarting the checkout."""
    question = " ".join((question or "").split())
    customer_text = (
        "Isa necesita confirmar algo antes de aprobar tu compra:\n\n{}"
    ).format(question)
    if not send_whatsapp_text(action["customer_phone"], customer_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "No pude enviarle la pregunta; el pendiente sigue abierto. Probá de nuevo.",
        )
        return
    conversation_id = action.get("conversation_id")
    if conversation_id:
        record_bot_message(conversation_id, customer_text)
        set_conversation_state(conversation_id, "BOT")
    try:
        # Not awaiting Isa any more -- now we are waiting on the customer, and
        # the case must stay pending so her reply can be attached to it.
        clear_isa_awaiting(action["id"])
        set_isa_awaiting(action["id"], "customer_answer")
    except Exception as error:  # noqa: BLE001
        print("ERROR marcando espera de respuesta (tipo: {}).".format(type(error).__name__))
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Listo, se lo pregunté. La compra queda abierta y te aviso apenas responda 😊",
    )


def _deliver_isa_response(action: dict, message_text: str, keep_open: bool = False) -> bool:
    """Deliver reviewed owner context, then return the chat to normal BOT mode."""
    customer_text = _isa_customer_instruction(message_text, action) or message_text
    if customer_text == "__SEND_SPECIAL_CONDITIONS__":
        _send_special_sale_conditions(action)
        return True
    if keep_open:
        # She is taking the chat: one clear transition, then her words exactly
        # as written. No "Isa me respondió:" on every message once the customer
        # already knows who they are talking to.
        customer_text = "Te dejo con Isa por acá 😊\n\n{}".format(customer_text)
    else:
        # A one-off answer must be attributable, or it reads as Fred's own.
        # Her instruction wrapper is stripped so she can write "decile que..."
        # and the customer still reads a normal message.
        customer_text = "Isa me respondió:\n\n{}".format(_reason_for_customer(customer_text))

    if not send_whatsapp_text(action["customer_phone"], customer_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "No pude enviar esa respuesta a la clienta; no la pierdo. Probá mandarla de nuevo.",
        )
        return False

    if keep_open:
        # Isa takes the thread. From here Fred stops answering entirely and
        # only carries messages in both directions, until she hands it back.
        set_isa_awaiting(action["id"], ISA_OWNS_KIND)
        record_bot_message(action["conversation_id"], customer_text)
        set_conversation_state(action["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, se lo mandé y quedás a cargo de este chat: todo lo que escriba "
            "te lo reenvío acá y Fred no interviene. Escribime lo que quieras "
            "decirle y se lo paso tal cual. Cuando termines, mandá "
            "“devolver a Fred”.",
        )
        return True

    result = resolve_pending_action(action["id"], "approved")
    if not result:
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "La respuesta llegó a la clienta, pero el pendiente cambió de estado. Revisalo antes de seguir.",
        )
        return False
    set_conversation_state(result["conversation_id"], "BOT")
    record_bot_message(result["conversation_id"], customer_text)
    send_whatsapp_text(
        ISA_WHATSAPP_NUMBER,
        "Listo, Fred le pasó tu respuesta a la clienta y sigue atendiendo ese chat 😊",
    )
    if pending_action_count():
        send_next_pending_to_isa()
    return True


def _create_demo_link_for_approved_sale(action: dict) -> dict:
    """Create a checkout only in the dedicated demo store.

    The real product is intentionally never copied to the demo order. This
    verifies approval -> checkout plumbing with a clearly fake test SKU.
    """
    if not DEMO_APPROVALS_ENABLED:
        raise DraftOrderDemoError("La aprobación demo está apagada.")
    if action["action_type"] != "purchase_review":
        raise DraftOrderDemoError("Solo las fichas de compra pueden crear un link demo.")

    sale_draft = action.get("payload", {}).get("sale_draft", {})
    try:
        quantity = int(sale_draft.get("items_status", "").split("×", 1)[0].strip())
    except (ValueError, AttributeError):
        raise DraftOrderDemoError("No pude leer la cantidad de la ficha.")

    return create_demo_draft_order("TEST-FRED-001", quantity)


def handle_isa_message(
    message_text: str,
    wa_message_id: str = "",
    button_reply_id: str = "",
) -> None:
    """Any message from Isa opens the queue; button replies resolve one draft."""
    feedback = _isa_feedback_text(message_text)
    if feedback:
        try:
            saved = record_isa_feedback(
                ISA_WHATSAPP_NUMBER,
                feedback,
                wa_message_id=wa_message_id or None,
            )
            if saved:
                send_whatsapp_text(
                    ISA_WHATSAPP_NUMBER,
                    "Listo, guardé tu feedback para revisarlo. No cambia nada automáticamente.",
                )
            else:
                print("[Isa] Feedback duplicado ignorado.")
        except Exception as error:  # noqa: BLE001
            print(f"ERROR guardando feedback de Isa (tipo: {type(error).__name__})")
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude guardar ese feedback ahora. Probá enviarlo de nuevo más tarde.",
            )
        return

    # Explaining the options must never touch a case, so this runs before
    # every handler that could act on one -- including the paths that deliver
    # Isa's text straight to a customer.
    if not button_reply_id and _isa_asks_for_legend(message_text):
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, ISA_OPTIONS_LEGEND)
        return

    if _handle_isa_reminder_request(message_text):
        return

    if _handle_isa_operations_summary_request(message_text):
        return

    if _handle_isa_quality_review_request(message_text):
        return

    # Isa can use a natural instruction instead of hunting for the card button.
    # It only acts when an actual encargo/cotización is pending.
    if not button_reply_id and _is_special_conditions_request(message_text):
        special_pending = next(
            (
                action
                for action in list_pending_actions(limit=20)
                if action["action_type"] == "special_sale_request"
            ),
            None,
        )
        if special_pending:
            _send_special_sale_conditions(special_pending)
            return

    # Isa can also write a simple instruction such as "mandar políticas".
    # No card hunting is required for this safe, pre-defined delivery.
    if not button_reply_id:
        customer_pending = next(
            (
                action
                for action in list_pending_actions(limit=20)
                if action["action_type"] in (
                    "bot_fallback",
                    "human_handoff",
                    "special_sale_request",
                )
            ),
            None,
        )
        if customer_pending and _isa_customer_instruction(message_text, customer_pending):
            _deliver_isa_response(customer_pending, message_text)
            return

    response_match = re.match(r"^reply_to_fred:(\d+)$", button_reply_id or "")
    if response_match:
        action_id = int(response_match.group(1))
        pending_action = _pending_action_by_id(action_id)
        if not pending_action or pending_action["action_type"] not in (
            "bot_fallback",
            "human_handoff",
            "special_sale_request",
        ):
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Esa consulta ya no está disponible.")
            return
        if wait_for_isa_response(action_id):
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Perfecto. Escribime la respuesta o dato que querés que Fred le comunique a la clienta. "
                "Fred se la envía y después retoma el chat.",
            )
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No pude preparar esa consulta para responder ahora.")
        return

    # Isa chose an option that needs her to type something (rejection reason,
    # question for the customer, reply that keeps a consultation open). That
    # message is answered against THAT case, never guessed from its text.
    awaiting_kind_action = next(
        (
            item for item in list_pending_actions(limit=20)
            if item.get("payload", {}).get("awaiting_isa_kind")
        ),
        None,
    )
    # While Isa owns a thread her plain text IS the customer's message: it is
    # relayed exactly as written, never rewritten and never passed through the
    # model. Only an explicit hand-back ends the session.
    owned = _isa_owned_case()
    if owned and not button_reply_id:
        if re.search(
            r"\b(devolver\s+a\s+fred|devolvele?\s+a\s+fred|que\s+siga\s+fred|"
            r"termin(?:ar|o|é)\s+(?:la\s+)?asesor|listo\s+fred|fred\s+segu[ií])\b",
            _normalized_text(message_text),
        ):
            _hand_thread_back_to_fred(owned)
            return
        if send_whatsapp_text(owned["customer_phone"], message_text):
            record_bot_message(owned["conversation_id"], message_text)
            print("[Isa] Mensaje entregado tal cual a la clienta.")
        else:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude entregarle ese mensaje. Probá de nuevo, no lo pierdo.",
            )
        return

    if awaiting_kind_action and not button_reply_id:
        # A help request was already answered and returned far above, so
        # anything reaching here is a real answer to the open case.
        kind = awaiting_kind_action["payload"]["awaiting_isa_kind"]
        if kind == "reject_purchase":
            _reject_purchase_with_reason(awaiting_kind_action, message_text)
            return
        if kind == "ask_customer":
            _ask_customer_for_purchase(awaiting_kind_action, message_text)
            return
        if kind == "reply_keep_open":
            _deliver_isa_response(awaiting_kind_action, message_text, keep_open=True)
            return

    # Isa's text is intentionally handled before her own internal-sale draft:
    # once she chose “Responder a Fred”, her next message belongs to the
    # customer consultation and is delivered verbatim as reviewed information.
    awaiting_response = next(
        (
            action
            for action in list_pending_actions(limit=20)
            if action["action_type"] in (
                "bot_fallback",
                "human_handoff",
                "special_sale_request",
            )
            and action.get("payload", {}).get("awaiting_isa_response")
        ),
        None,
    )
    if awaiting_response and not button_reply_id:
        normalized = _normalized_text(message_text).strip()
        if re.fullmatch(r"(?:cancelar|cancelalo|dejalo)", normalized):
            result = resolve_pending_action(awaiting_response["id"], "rejected")
            if result:
                set_conversation_state(result["conversation_id"], "BOT")
                send_whatsapp_text(
                    ISA_WHATSAPP_NUMBER,
                    "Dale, no envié ninguna respuesta de Isa. Fred vuelve a atender a la clienta.",
                )
            return

        _deliver_isa_response(awaiting_response, message_text)
        return

    if _handle_isa_sale_session(message_text, button_reply_id):
        return

    if _looks_like_isa_sale_request(message_text):
        start_isa_sale_session(ISA_WHATSAPP_NUMBER)
        send_isa_sale_type_menu()
        return

    demo_order_request = _isa_demo_order_request(message_text)
    if demo_order_request:
        sku, quantity = demo_order_request
        try:
            draft_order = create_demo_draft_order(sku, quantity)
        except DraftOrderDemoError as error:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No creé ninguna orden. {}".format(error),
            )
            return

        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Link demo creado ✅\nProducto: {}\nCantidad: {}\nBorrador #{}\n{}\n\n"
            "Es solo una prueba: no se lo envíes a una clienta ni lo uses para cobrar."
            .format(
                draft_order["product_name"],
                draft_order["quantity"],
                draft_order["id"],
                draft_order["checkout_url"],
            ),
        )
        print("[Isa] Borrador demo #{} creado.".format(draft_order["id"]))
        return

    if _is_demo_command(message_text):
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Para la prueba demo escribí exactamente: demo: TEST-FRED-001 x 1\n"
            "No se crea nada si el modo demo está apagado.",
        )
        return

    match = re.match(
        r"^(approve|approve_demo|approve_checkout|send_special_conditions|reject|view|"
        r"take_handoff|pause_bot|resume_bot|reject_purchase|ask_customer|reply_keep_open|"
        r"close_consultation|return_to_fred|contact_customer):(\d+)$",
        button_reply_id or "",
    )
    if not match:
        send_next_pending_to_isa()
        return

    action, action_id_text = match.groups()
    action_id = int(action_id_text)

    if action in ("reject_purchase", "ask_customer", "reply_keep_open"):
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        if action == "reject_purchase" and pending_action["action_type"] != "purchase_review":
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente no es una compra para rechazar.")
            return
        prompts = {
            "reject_purchase": (
                "Dale. Contame en un mensaje por qué no podemos avanzar (por ejemplo "
                "“no hay stock para esa cantidad”) y se lo explico a la clienta con mis "
                "palabras. No genero ningún link.",
            ),
            "ask_customer": (
                "Dale. Escribime qué necesitás preguntarle y se lo pregunto. La compra "
                "queda abierta esperando su respuesta y te aviso apenas conteste.",
            ),
            "reply_keep_open": (
                "Dale. Escribime la respuesta y se la paso. La consulta queda abierta "
                "para seguir el ida y vuelta.",
            ),
        }
        if set_isa_awaiting(action_id, action):
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, prompts[action][0])
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "No pude preparar ese pendiente ahora.")
        return

    if action == "contact_customer":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        phone = pending_action["customer_phone"]
        aviso = (
            "Isa quiere comentarte unas cosas antes de que completes la compra. "
            "Te va a escribir directamente por WhatsApp en un momento 😊"
        )
        if send_whatsapp_text(phone, aviso):
            record_bot_message(pending_action["conversation_id"], aviso)
        # No es un rechazo: la compra no se aprueba pero tampoco se descarta.
        try:
            save_pending_action_resolution(action_id, "human_contact")
        except Exception as error:  # noqa: BLE001
            print("ERROR guardando resolución human_contact (tipo: {}).".format(type(error).__name__))
        resolve_pending_action(action_id, "rejected")
        set_conversation_state(pending_action["conversation_id"], "BOT")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Perfecto. Podés seguir directamente con la clienta:\n+{}".format(
                re.sub(r"\D", "", phone),
            ),
        )
        return

    if action == "return_to_fred":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        _hand_thread_back_to_fred(pending_action)
        return

    if action == "close_consultation":
        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
            return
        set_conversation_state(result["conversation_id"], "BOT")
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Listo, cerré la consulta. Fred sigue atendiendo 😊")
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action == "view":
        actions = [item for item in list_pending_actions(limit=20) if item["id"] == action_id]
        if actions:
            send_isa_pending_buttons(actions[0])
        else:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
        return

    if action == "send_special_conditions":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        _send_special_sale_conditions(pending_action)
        return

    if action == "approve_demo":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        try:
            draft_order = _create_demo_link_for_approved_sale(pending_action)
        except DraftOrderDemoError as error:
            # La tarjeta queda pendiente para que Isa pueda corregir o descartar.
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No creé ningún link demo. {}".format(error),
            )
            return

        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "El link demo se creó, pero el pendiente ya había sido resuelto. No se envió a la clienta.",
            )
            return

        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Link demo creado tras tu aprobación ✅\nBorrador #{}\n{}\n\n"
            "Usa TEST-FRED-001 y no corresponde al producto real ni se envía a la clienta."
            .format(draft_order["id"], draft_order["checkout_url"]),
        )
        print("[Isa] Borrador demo #{} creado desde pendiente #{}.".format(draft_order["id"], action_id))
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action in ("take_handoff", "pause_bot"):
        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
            return
        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, Fred queda en pausa para esta clienta. En el número de prueba todavía no hay una bandeja "
            "compartida para que respondas como Isa; esa capa llega con el número oficial y coexistencia.",
        )
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action == "resume_bot":
        result = resolve_pending_action(action_id, "rejected")
        if not result:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
            return
        set_conversation_state(result["conversation_id"], "BOT")
        customer_text = (
            "Dale, sigo por acá 😊 Ya tengo presente la consulta que veníamos viendo. "
            "Si querés, retomamos desde ahí; también podés contarme cualquier otra cosa."
        )
        if send_whatsapp_text(result["payload"].get("customer_phone", ""), customer_text):
            record_bot_message(result["conversation_id"], customer_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Listo, Fred retoma esa conversación.",
        )
        if pending_action_count():
            send_next_pending_to_isa()
        return

    if action == "approve_checkout":
        pending_action = _pending_action_by_id(action_id)
        if not pending_action:
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya no está disponible.")
            return
        if pending_action["action_type"] != "purchase_review":
            send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente no es una compra para aprobar.")
            return

        sale_draft = pending_action.get("payload", {}).get("sale_draft", {})
        sku = str(sale_draft.get("selected_sku") or "").strip()
        # Revalidate the SAME identity the card showed. The sale itself happens
        # in Tiendanube: Fred sends the real product link instead of rebuilding
        # a checkout, so the store handles cart, data, shipping and payment.
        try:
            fresh = get_stock(sku) if sku else {}
        except Exception as error:  # noqa: BLE001
            print("ERROR revalidando SKU aprobado (tipo: {}).".format(type(error).__name__))
            fresh = {}
        integrity = _purchase_draft_integrity_error({
            "selected_sku": sku,
            "quantity": _draft_quantity(sale_draft),
        })
        if integrity:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "{}\n\nNo mandé ningún link y el pendiente sigue abierto.".format(
                    _classify_checkout_failure(sale_draft, RuntimeError(integrity)),
                ),
            )
            return
        product_url = _product_url_for_sku(sku)
        if not product_url:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude obtener el link público de ese producto. El pendiente sigue abierto.",
            )
            return

        customer_text = (
            "¡Listo! Isa aprobó tu compra 😊\n\n"
            "Podés completarla directamente en Beauty House acá:\n{}"
        ).format(product_url)
        if not send_whatsapp_text(pending_action["customer_phone"], customer_text):
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "No pude entregarle el link a la clienta. Tocá “Aprobar” otra vez.",
            )
            return

        result = resolve_pending_action(action_id, "approved")
        if not result:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "El link ya fue enviado a la clienta, pero el pendiente cambió de estado. Revisalo en Tiendanube.",
            )
            return
        set_conversation_state(result["conversation_id"], "BOT")
        record_bot_message(result["conversation_id"], customer_text)
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Compra aprobada ✅ Le mandé el link del producto. La compra se completa "
            "en Tiendanube: ahí carga sus datos, elige envío y paga.",
        )
        print("[Isa] Link de producto enviado desde pendiente #{}.".format(action_id))
        if pending_action_count():
            send_next_pending_to_isa()
        return

    result = resolve_pending_action(
        action_id,
        "approved" if action == "approve" else "rejected",
    )
    if not result:
        send_whatsapp_text(ISA_WHATSAPP_NUMBER, "Ese pendiente ya fue resuelto.")
        return

    if action == "approve":
        set_conversation_state(result["conversation_id"], "ISA")
        send_whatsapp_text(
            ISA_WHATSAPP_NUMBER,
            "Tomaste el caso #{}. Fred deja de responderle a la clienta. Todavía "
            "no se creó ninguna orden: es solo el borrador de trabajo para esta prueba."
            .format(action_id),
        )
    else:
        set_conversation_state(result["conversation_id"], "BOT")
        # A cancelled purchase review must also close the loop with the
        # customer.  Otherwise the internal card disappears but the customer
        # remains waiting for an approval that will never arrive.
        if result["action_type"] == "purchase_review":
            customer_text = (
                "Isa revisó la preparación y decidió no avanzar con este link por ahora. "
                "No tenés que pagar nada ni hay una compra confirmada 😊\n\n"
                "Si querés, seguimos viendo alternativas por acá. Y si preferís hablar "
                "directamente con Isa, decime y se lo paso."
            )
            customer_phone = result["payload"].get("customer_phone", "")
            if customer_phone and send_whatsapp_text(customer_phone, customer_text):
                record_bot_message(result["conversation_id"], customer_text)
                customer_notice = " Fred ya le avisó a la clienta y sigue atendiendo ese chat."
            else:
                customer_notice = " No pude avisarle a la clienta; revisá el chat antes de seguir."
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Cancelaste la compra pendiente #{}.{}".format(action_id, customer_notice),
            )
        else:
            send_whatsapp_text(
                ISA_WHATSAPP_NUMBER,
                "Cancelaste el pendiente #{}. Fred vuelve a atender a la clienta."
                .format(action_id),
            )

    if pending_action_count():
        send_next_pending_to_isa()


# ============================================================
# WEBHOOK — VERIFICACIÓN META
# ============================================================


def _require_dashboard(credentials: HTTPBasicCredentials = Depends(dashboard_security)) -> str:
    """Protect owner-only operational data without exposing it in a public route."""
    if not ADMIN_DASHBOARD_USERNAME or not ADMIN_DASHBOARD_PASSWORD or not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Panel privado no autorizado",
            headers={"WWW-Authenticate": "Basic"},
        )
    valid_user = secrets.compare_digest(credentials.username, ADMIN_DASHBOARD_USERNAME)
    valid_password = secrets.compare_digest(credentials.password, ADMIN_DASHBOARD_PASSWORD)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Panel privado no autorizado",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _dashboard_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        """<!doctype html><html lang="es"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{}</title><style>
        body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#fafafa;color:#17212b;max-width:1100px;margin:0 auto;padding:28px 18px}}
        h1{{margin:0 0 6px}} .muted{{color:#68727c}} .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}}
        .card{{background:#fff;border:1px solid #e4e7eb;border-radius:12px;padding:15px;min-width:130px}} .value{{font-size:28px;font-weight:700}}
        table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e4e7eb}} th,td{{padding:12px;text-align:left;border-bottom:1px solid #eef0f2;vertical-align:top}}
        a{{color:#1565c0}} .bubble{{max-width:76%;margin:10px 0;padding:11px 13px;border-radius:12px;background:#fff;border:1px solid #e4e7eb;white-space:pre-wrap}}
        .out{{margin-left:auto;background:#e6f7e9}} .meta{{font-size:12px;color:#68727c;margin-bottom:4px}}
        </style></head><body>{}</body></html>""".format(html.escape(title), body),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _format_dashboard_time(value) -> str:
    if not value:
        return "—"
    return value.astimezone(ARGENTINA_TZ).strftime("%d/%m %H:%M")


def _dashboard_csrf_token(username: str, action: str) -> str:
    """Same-day action token; a public page cannot forge an admin POST."""
    message = "{}:{}:{}".format(username, action, datetime.now(ARGENTINA_TZ).date()).encode("utf-8")
    return hmac.new(
        ADMIN_DASHBOARD_PASSWORD.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()


def _dashboard_observability_box(snapshot: dict) -> str:
    """Render aggregate agent signals only; chats remain in their own view."""
    labels = {
        "reply": "Respuestas resueltas",
        "clarify_product": "Precisiones de producto",
        "start_sales_intake": "Compras iniciadas",
        "handoff_to_isa": "Casos enviados a Isa",
        "service_fallback": "Respuestas seguras",
    }
    actions = snapshot.get("actions") or {}
    action_text = ", ".join(
        "{}: {}".format(html.escape(labels.get(action, action)), amount)
        for action, amount in actions.items()
    ) or "Todavía no hay turnos del agente registrados."
    cards = "".join(
        '<div class="card"><div class="value">{}</div><div>{}</div></div>'.format(
            value, label
        )
        for value, label in (
            (snapshot.get("turns", 0), "Turnos de Fred"),
            ("{} ms".format(snapshot.get("average_duration_ms", 0)), "Demora promedio"),
            (snapshot.get("average_tokens", 0), "Tokens promedio"),
            (snapshot.get("service_fallbacks", 0), "Respuestas seguras"),
        )
    )
    return (
        "<h2>Calidad y rendimiento de Fred</h2>"
        '<p class="muted">Agregados de las últimas 24 horas; no incluye texto ni datos de clientas.</p>'
        '<div class="cards">{}</div><p><strong>Decisiones:</strong> {}</p>'
    ).format(cards, action_text)


@app.get("/admin", response_class=HTMLResponse)
async def operations_dashboard(request: Request, username: str = Depends(_require_dashboard)):
    """Simple owner-only view of Fred's real conversations and operational state."""
    snapshot = dashboard_snapshot()
    try:
        observability = agent_observability_snapshot()
    except Exception as error:  # noqa: BLE001
        print("ERROR leyendo observabilidad del panel (tipo: {}).".format(type(error).__name__))
        observability = {}
    counts = snapshot["last_24h"]
    labels = {
        "active_conversations": "Conversaciones activas",
        "customer_messages": "Mensajes de clientas",
        "fred_messages": "Mensajes de Fred",
        "pending_actions": "Pendientes de Isa",
        "approved_checkouts": "Checkouts aprobados",
        "fred_paid_orders": "Pagos confirmados",
    }
    cards = "".join(
        '<div class="card"><div class="value">{}</div><div>{}</div></div>'.format(
            counts[key], labels[key]
        )
        for key in labels
    )
    pending = snapshot["pending_by_type"]
    pending_labels = {
        "purchase_review": "Compras para aprobar",
        "human_handoff": "Clientas que pidieron a Isa",
        "special_sale_request": "Encargos o mayoristas",
        "bot_fallback": "Casos que Fred no pudo resolver",
    }
    pending_text = ", ".join(
        "{}: {}".format(html.escape(pending_labels.get(str(kind), str(kind))), amount)
        for kind, amount in pending.items()
    ) or "Sin pendientes"
    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href=\"/admin/conversations/{}\">Ver chat</a></td></tr>".format(
            html.escape(str(item["customer_phone"])),
            html.escape(str(item["state"])),
            html.escape(_format_dashboard_time(item["last_message_at"])),
            html.escape(str(item["last_message"])[:160]),
            item["id"],
        )
        for item in snapshot["conversations"]
    ) or '<tr><td colspan="5">Todavía no hay conversaciones.</td></tr>'
    connection_message = request.query_params.get("connection", "")
    connection_box = ""
    if connection_message == "created":
        connection_box = '<p><strong>Listo:</strong> Tiendanube quedó conectada para avisar pagos.</p>'
    elif connection_message == "exists":
        connection_box = '<p><strong>Ya estaba conectado:</strong> no se creó un webhook duplicado.</p>'
    elif connection_message == "error":
        connection_box = '<p><strong>No se pudo conectar Tiendanube.</strong> Revisá que la autorización OAuth siga vigente e intentá de nuevo.</p>'
    webhook_box = (
        '<h2>Pagos de Tiendanube</h2><p class="muted">Estado de recepción: {}.</p>'
        '<form method="post" action="/admin/tiendanube/connect-payments">'
        '<input type="hidden" name="csrf" value="{}">'
        '<button type="submit">Conectar avisos de pago</button></form>'
    ).format(
        "activo" if TIENDANUBE_WEBHOOKS_ENABLED else "apagado (primero agregá TIENDANUBE_WEBHOOKS_ENABLED=true en Railway)",
        _dashboard_csrf_token(username, "connect-payments"),
    )
    catalog_audit_box = (
        '<h2>Salud del catálogo</h2><p class="muted">Revisa riesgos de venta sin cambiar Tiendanube.</p>'
        '<form method="post" action="/admin/tiendanube/catalog-audit">'
        '<input type="hidden" name="csrf" value="{}">'
        '<button type="submit">Auditar catálogo</button></form>'
    ).format(_dashboard_csrf_token(username, "catalog-audit"))
    return _dashboard_page(
        "Fred | Operación",
        "<h1>Operación de Fred</h1><p class=\"muted\">Datos reales de las últimas 24 horas. Solo para Isa/equipo autorizado.</p>"
        '<div class="cards">{}</div><p><strong>Cola pendiente:</strong> {}</p>{}{}'
        "{}<h2>Conversaciones recientes</h2><table><thead><tr><th>Cliente</th><th>Estado</th><th>Último mensaje</th><th>Vista previa</th><th></th></tr></thead><tbody>{}</tbody></table>".format(
            cards,
            pending_text,
            _dashboard_observability_box(observability),
            connection_box,
            webhook_box + catalog_audit_box,
            rows,
        ),
    )


@app.post("/admin/tiendanube/connect-payments")
async def connect_tiendanube_payments(request: Request, username: str = Depends(_require_dashboard)):
    """Owner-clicked, idempotent webhook registration; no terminal or token copying."""
    body = (await request.body()).decode("utf-8", errors="replace")
    csrf = parse_qs(body).get("csrf", [""])[0]
    expected = _dashboard_csrf_token(username, "connect-payments")
    if not secrets.compare_digest(csrf, expected):
        raise HTTPException(status_code=403, detail="Acción privada no verificada")
    try:
        result = register_order_paid_webhook(PUBLIC_BASE_URL + "/webhooks/tiendanube")
        outcome = "created" if result["created"] else "exists"
        print("[Tiendanube] Webhook order/paid registrado: {}.".format(result.get("id", "sin id")))
    except Exception as error:  # noqa: BLE001
        print("ERROR registrando webhook Tiendanube (tipo: {}).".format(type(error).__name__))
        outcome = "error"
    return RedirectResponse("/admin?connection=" + outcome, status_code=303)


@app.post("/admin/tiendanube/catalog-audit", response_class=HTMLResponse)
async def audit_tiendanube_catalog(request: Request, username: str = Depends(_require_dashboard)):
    """Run an owner-triggered, read-only catalog health report."""
    body = (await request.body()).decode("utf-8", errors="replace")
    csrf = parse_qs(body).get("csrf", [""])[0]
    expected = _dashboard_csrf_token(username, "catalog-audit")
    if not secrets.compare_digest(csrf, expected):
        raise HTTPException(status_code=403, detail="Acción privada no verificada")
    try:
        audit = catalog_health_audit()
    except Exception as error:  # noqa: BLE001
        print("ERROR auditando catálogo (tipo: {}).".format(type(error).__name__))
        return _dashboard_page(
            "Fred | Auditoría de catálogo",
            '<p><a href="/admin">← Volver al panel</a></p><h1>No pude leer el catálogo</h1>'
            '<p>La auditoría no modificó nada. Revisá la conexión de Tiendanube e intentá de nuevo.</p>',
        )

    labels = {
        "products_scanned": "Productos revisados",
        "variants_scanned": "Variantes revisadas",
        "published_without_sku": "Publicadas sin SKU",
        "published_untracked_stock": "Publicadas sin stock controlado",
        "hidden_with_positive_stock": "Ocultas con stock positivo",
        "published_out_of_stock": "Publicadas sin stock",
        "duplicate_skus": "SKU duplicados",
    }
    rows = "".join(
        "<tr><td>{}</td><td>{}</td></tr>".format(
            html.escape(label), audit["totals"][key]
        )
        for key, label in labels.items()
    )
    examples = "".join(
        "<h3>{}</h3><p>{}</p>".format(
            html.escape(labels[key]),
            html.escape(" · ".join(values) or "Sin ejemplos"),
        )
        for key, values in audit["samples"].items()
    )
    return _dashboard_page(
        "Fred | Auditoría de catálogo",
        '<p><a href="/admin">← Volver al panel</a></p><h1>Auditoría de catálogo</h1>'
        '<p class="muted">Solo lectura: este reporte no modificó productos ni stock.</p>'
        '<table><thead><tr><th>Señal</th><th>Cantidad</th></tr></thead><tbody>{}</tbody></table>{}'.format(
            rows, examples
        ),
    )


@app.get("/admin/conversations/{conversation_id}", response_class=HTMLResponse)
async def operations_conversation(conversation_id: int, _: str = Depends(_require_dashboard)):
    conversation = dashboard_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    bubbles = "".join(
        '<div class="bubble {}"><div class="meta">{} · {}</div>{}</div>'.format(
            "out" if message["direction"] == "out" else "",
            html.escape(str(message["sender"])),
            html.escape(_format_dashboard_time(message["created_at"])),
            html.escape(str(message["body"])),
        )
        for message in conversation["messages"]
    ) or '<p class="muted">Sin mensajes guardados.</p>'
    return _dashboard_page(
        "Fred | Conversación",
        '<p><a href="/admin">← Volver al panel</a></p><h1>Cliente {}</h1><p class="muted">Estado: {}</p>{}'.format(
            html.escape(str(conversation["customer_phone"])),
            html.escape(str(conversation["state"])),
            bubbles,
        ),
    )


def _process_tiendanube_paid_order(event_key: str, order_id: str) -> None:
    """Verify a paid order, link it to Fred, and record the result once."""
    try:
        fetch_paid_order(order_id)
        checkout = fred_checkout_for_order(order_id)
        if not checkout:
            finish_tiendanube_event(event_key, "ignored")
            print("[Tiendanube] Pago {} no pertenece a un checkout de Fred.".format(order_id))
            return

        # A client may pay outside the WhatsApp 24-hour window. Only an approved
        # template is valid then; free-form delivery would be unreliable.
        if PAYMENT_CONFIRMED_TEMPLATE_NAME:
            if send_whatsapp_template(
                checkout["customer_phone"],
                PAYMENT_CONFIRMED_TEMPLATE_NAME,
                PAYMENT_CONFIRMED_TEMPLATE_LANGUAGE,
                [order_id],
            ):
                record_bot_message(
                    checkout["conversation_id"],
                    "[Pago confirmado por Tiendanube: orden #{}]".format(order_id),
                )
        finish_tiendanube_event(event_key, "processed")
        print("[Tiendanube] Pago confirmado para orden #{}.".format(order_id))
    except Exception as error:  # noqa: BLE001
        print("ERROR procesando pago de Tiendanube (tipo: {}).".format(type(error).__name__))
        try:
            finish_tiendanube_event(event_key, "failed", type(error).__name__)
        except Exception:  # noqa: BLE001
            print("ERROR guardando fallo de pago de Tiendanube.")


@app.post("/webhooks/tiendanube")
async def tiendanube_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Tiendanube order events with HMAC validation and idempotence."""
    raw_body = await request.body()
    signature = request.headers.get("x-linkedstore-hmac-sha256", "")
    if not webhook_signature_is_valid(raw_body, signature):
        raise HTTPException(status_code=401, detail="Firma de Tiendanube inválida")
    if not TIENDANUBE_WEBHOOKS_ENABLED:
        return JSONResponse(content={"ok": True, "status": "disabled"}, status_code=202)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Evento de Tiendanube inválido")

    store_id = str(payload.get("store_id", "")).strip()
    event_name = str(payload.get("event", "")).strip()
    order_id = str(payload.get("id", "")).strip()
    expected_store = os.getenv("TIENDANUBE_STORE_ID", "").strip()
    if store_id != expected_store:
        raise HTTPException(status_code=403, detail="Tienda de Tiendanube no autorizada")
    if event_name != "order/paid" or not order_id:
        return JSONResponse(content={"ok": True, "status": "ignored"}, status_code=202)

    event_key = "{}:{}:{}".format(store_id, event_name, order_id)
    if not claim_tiendanube_event(event_key, store_id, event_name, order_id, payload):
        return JSONResponse(content={"ok": True, "status": "duplicate"})
    background_tasks.add_task(_process_tiendanube_paid_order, event_key, order_id)
    return JSONResponse(content={"ok": True, "status": "accepted"}, status_code=202)


@app.get("/documents/preventa-encargos.pdf")
async def download_encargos_policy() -> FileResponse:
    """Expose the owner-approved encargo PDF for WhatsApp document delivery."""
    if not ENCARGOS_PDF_PATH.is_file():
        return JSONResponse(content={"error": "Documento no disponible"}, status_code=503)
    return FileResponse(
        path=ENCARGOS_PDF_PATH,
        media_type="application/pdf",
        filename="Beauty-House-Preventa-y-Encargos.pdf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ============================================================
# TIENDANUBE — CONEXIÓN OAUTH ASISTIDA
# ============================================================

def _oauth_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Render a small, non-cacheable status page without exposing credentials."""

    document = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:680px;margin:48px auto;line-height:1.5;padding:0 20px">
<h1>{title}</h1>{body}</body></html>""".format(
        title=html.escape(title), body=body
    )
    return HTMLResponse(
        content=document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/tiendanube/connect")
async def tiendanube_connect():
    """Start a safe owner-authorized Tiendanube OAuth connection."""

    client_id = os.getenv("TIENDANUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    # The generic Tiendanube authorization URL can reuse the Partner demo-store
    # session. Start from the real store's admin domain instead, so the owner
    # authorizes the intended store while the state cookie still protects the
    # callback.
    store_domain = os.getenv(
        "TIENDANUBE_STORE_DOMAIN", "beautyhouse5.mitiendanube.com"
    ).strip().lower()
    if not client_id or not client_secret:
        return _oauth_page(
            "Conexión no configurada",
            "<p>Faltan las credenciales de la app de Tiendanube en Railway.</p>",
            503,
        )
    if not re.fullmatch(r"[a-z0-9-]+\.mitiendanube\.com", store_domain):
        return _oauth_page(
            "Dominio de tienda inválido",
            "<p>Revisá <code>TIENDANUBE_STORE_DOMAIN</code> en Railway.</p>",
            503,
        )

    state = secrets.token_urlsafe(32)
    response = RedirectResponse(
        "https://{}/admin/apps/{}/authorize?state={}".format(
            store_domain, client_id, state
        ),
        status_code=302,
    )
    response.set_cookie(
        "fred_tiendanube_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/tiendanube/oauth/callback")
async def tiendanube_oauth_callback(request: Request):
    """Exchange an authorization code and store the token encrypted."""

    client_id = os.getenv("TIENDANUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("TIENDANUBE_CLIENT_SECRET", "").strip()
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    expected_state = request.cookies.get("fred_tiendanube_oauth_state", "")
    if not client_id or not client_secret:
        return _oauth_page("Conexión no configurada", "<p>Faltan credenciales de la app en Railway.</p>", 503)
    if not code or not expected_state or not secrets.compare_digest(state, expected_state):
        return _oauth_page(
            "No se pudo verificar la conexión",
            "<p>Volvé a iniciar desde el link de conexión de Fred.</p>",
            400,
        )

    try:
        token_response = requests.post(
            "https://www.tiendanube.com/apps/authorize/token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=15,
        )
        payload = token_response.json()
    except requests.RequestException:
        print("[Tiendanube OAuth] No se pudo contactar el endpoint de autorización.")
        return _oauth_page(
            "No se pudo conectar Tiendanube",
            "<p>No se pudo contactar Tiendanube. Probá nuevamente en unos minutos.</p>",
            502,
        )
    except ValueError:
        print("[Tiendanube OAuth] Respuesta inválida del canje.")
        return _oauth_page(
            "Respuesta inválida de Tiendanube",
            "<p>Volvé a iniciar desde el link de conexión de Fred.</p>",
            400,
        )

    if not token_response.ok:
        print(
            "[Tiendanube OAuth] Canje rechazado "
            "(HTTP {}).".format(token_response.status_code)
        )
        return _oauth_page(
            "Tiendanube rechazó la conexión",
            "<p>Revisá en Railway que <code>TIENDANUBE_CLIENT_ID</code> sea 38765 "
            "y que <code>TIENDANUBE_CLIENT_SECRET</code> corresponda a esta misma app.</p>",
            400,
        )

    try:
        store_id = str(payload.get("user_id", "")).strip()
        access_token = str(payload.get("access_token", "")).strip()
        expected_store_id = os.getenv("TIENDANUBE_STORE_ID", "").strip()
        if store_id != expected_store_id:
            print(
                "[Tiendanube OAuth] Tienda inesperada: {}.".format(store_id)
            )
            return _oauth_page(
                "Se conectó otra tienda",
                "<p>Tiendanube devolvió la tienda <strong>{}</strong>, pero Fred espera "
                "Beauty House (<strong>{}</strong>). No se modificó ninguna credencial.</p>".format(
                    html.escape(store_id or "sin identificador"),
                    html.escape(expected_store_id or "sin configurar"),
                ),
                400,
            )
        save_tiendanube_credential(
            store_id,
            access_token,
            str(payload.get("scope", "")),
        )
    except TiendanubeCredentialError:
        print("[Tiendanube OAuth] La credencial no pudo guardarse de forma segura.")
        return _oauth_page(
            "No se pudo guardar la conexión",
            "<p>Revisá las variables de Tiendanube y Supabase en Railway.</p>",
            400,
        )
    except psycopg2.Error:
        print("[Tiendanube OAuth] Supabase rechazó el guardado de la conexión.")
        return _oauth_page(
            "No se pudo guardar la conexión",
            "<p>La autorización fue válida, pero Fred no pudo guardarla en Supabase. "
            "Revisemos la conexión de Supabase.</p>",
            503,
        )

    response = _oauth_page(
        "Beauty House conectada",
        "<p>La autorización quedó guardada de forma cifrada y Fred ya puede usarla.</p>"
        "<p><strong>Tienda verificada:</strong> {}</p>".format(html.escape(store_id)),
    )
    response.delete_cookie("fred_tiendanube_oauth_state")
    return response

@app.get("/webhook")
async def webhook_get(request: Request):
    """Verificación del webhook por parte de Meta."""

    verify_token = request.query_params.get(
        "hub.verify_token"
    )

    challenge = request.query_params.get(
        "hub.challenge"
    )

    if verify_token == WHATSAPP_WEBHOOK_VERIFY_TOKEN:

        return JSONResponse(
            content=int(challenge)
        )

    return JSONResponse(
        content={"error": "Invalid token"},
        status_code=403,
    )


# ============================================================
# WEBHOOK — MENSAJES ENTRANTES
# ============================================================

def _ingest_durable_webhook(body: dict) -> None:
    """Persist every supported message; do not call Fred from the webhook."""
    for inbound in extract_inbound_messages(body):
        if _is_isa_phone(inbound.phone):
            # Isa's operational command path remains synchronous in M1.  The
            # customer path is the one that needs burst grouping and leasing.
            handle_isa_message(
                inbound.text,
                wa_message_id=inbound.wa_message_id,
                button_reply_id=inbound.interactive_reply_id,
            )
            continue
        result = enqueue_inbound_message(
            customer_phone=inbound.phone,
            body=inbound.text,
            wa_message_id=inbound.wa_message_id,
            provider_timestamp=inbound.provider_timestamp,
            quiet_seconds=MESSAGE_QUIET_WINDOW_SECONDS,
            max_burst_seconds=MESSAGE_MAX_BURST_WAIT_SECONDS,
        )
        if result["duplicate"]:
            print("[Cola] Mensaje duplicado ignorado: {}".format(inbound.wa_message_id))
        else:
            print(
                "[Cola] Mensaje {} en conversación {}, generation {}.".format(
                    inbound.wa_message_id,
                    result["conversation_id"],
                    result["generation"],
                )
            )


def _claim_payload(claim: dict, message_text: str) -> dict:
    """Build the narrow payload expected by the established Fred processor."""
    last_message = claim["messages"][-1]
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": claim["customer_phone"],
                        "id": last_message["wa_message_id"],
                        "text": {"body": message_text},
                    }]
                }
            }]
        }]
    }


async def _renew_claim_loop(claim: dict) -> None:
    interval = max(5.0, MESSAGE_LEASE_SECONDS / 3)
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(
            renew_processing_claim,
            claim["conversation_id"],
            claim["generation"],
            claim["lease_owner"],
            MESSAGE_LEASE_SECONDS,
        )
        if not renewed:
            return


async def _process_durable_claim(claim: dict) -> None:
    message_text = ordered_turn_text(claim["messages"])
    if not message_text:
        await asyncio.to_thread(
            finish_processing_claim,
            claim["conversation_id"],
            claim["generation"],
            claim["lease_owner"],
            claim["latest_message_id"],
            False,
        )
        return

    delivery_context = DeliveryContext(
        conversation_id=claim["conversation_id"],
        customer_phone=claim["customer_phone"],
        generation=claim["generation"],
        worker_id=claim["lease_owner"],
    )
    context_token = current_delivery_context.set(delivery_context)
    heartbeat = asyncio.create_task(_renew_claim_loop(claim))
    try:
        print(
            "[Cola] Procesando conversación {} generation {} ({} mensajes).".format(
                claim["conversation_id"], claim["generation"], len(claim["messages"])
            )
        )
        await _process_webhook_body(
            _claim_payload(claim, message_text), persisted_claim=claim
        )
        current = await asyncio.to_thread(
            processing_claim_is_current,
            claim["conversation_id"],
            claim["generation"],
            claim["lease_owner"],
        )
        if current:
            await asyncio.to_thread(
                finish_processing_claim,
                claim["conversation_id"],
                claim["generation"],
                claim["lease_owner"],
                claim["latest_message_id"],
                delivery_context.delivered,
            )
        else:
            await asyncio.to_thread(
                release_processing_claim,
                claim["conversation_id"],
                claim["lease_owner"],
            )
            print("[Cola] Turno obsoleto; queda pendiente la generation nueva.")
    except Exception as error:  # noqa: BLE001
        await asyncio.to_thread(
            release_processing_claim,
            claim["conversation_id"],
            claim["lease_owner"],
        )
        print("ERROR procesando cola durable (tipo: {}).".format(type(error).__name__))
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        current_delivery_context.reset(context_token)


async def _durable_message_worker_loop() -> None:
    while True:
        try:
            claim = await asyncio.to_thread(
                claim_next_conversation, _message_worker_id, MESSAGE_LEASE_SECONDS
            )
            if claim:
                await _process_durable_claim(claim)
                continue
        except Exception as error:  # noqa: BLE001
            print("ERROR en worker durable (tipo: {}).".format(type(error).__name__))
        await asyncio.sleep(MESSAGE_WORKER_POLL_SECONDS)

@app.post("/webhook")
async def webhook_post(request: Request):
    """Acknowledge Meta after durable ingestion when M1 is enabled."""
    body = await request.json()
    if DURABLE_MESSAGE_PROCESSING_ENABLED:
        await asyncio.to_thread(_ingest_durable_webhook, body)
        return JSONResponse(content={"ok": True})
    return await _process_webhook_body(body)


async def _process_webhook_body(body: dict, persisted_claim: Optional[dict] = None):
    """Existing Fred turn processor, reusable by the durable worker."""

    # Meta envía un array de entries
    if (
        "entry" not in body
        or not body["entry"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    entry = body["entry"][0]

    # Verificar cambios
    if (
        "changes" not in entry
        or not entry["changes"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    change = entry["changes"][0]

    # Verificar mensajes
    if (
        "value" not in change
        or "messages" not in change["value"]
    ):
        return JSONResponse(
            content={"ok": True}
        )

    messages = change["value"]["messages"]

    if not messages:
        return JSONResponse(
            content={"ok": True}
        )

    msg = messages[0]

    customer_phone = msg.get("from")
    wa_message_id = msg.get("id")
    interactive_reply = msg.get("interactive", {})
    button_reply_id = (
        interactive_reply.get("button_reply", {}).get("id", "")
        or interactive_reply.get("list_reply", {}).get("id", "")
    )

    message_text = (
        msg.get("text", {})
        .get("body", "")
        .strip()
    )

    if not message_text and button_reply_id:
        message_text = (
            interactive_reply.get("button_reply", {}).get("title", "")
            or interactive_reply.get("list_reply", {}).get("title", "")
        ).strip()

    if not message_text:

        return JSONResponse(
            content={"ok": True}
        )

    if _is_isa_phone(customer_phone):
        if _isa_feedback_text(message_text):
            print("\n[Isa] Feedback recibido.")
        else:
            print(f"\n[Isa] {message_text or button_reply_id}")
        handle_isa_message(
            message_text,
            wa_message_id=wa_message_id or "",
            button_reply_id=button_reply_id,
        )
        return JSONResponse(content={"ok": True})

    print(
        f"\n[WhatsApp] "
        f"{customer_phone}: "
        f"{message_text}"
    )

    conversation_id = 0
    state = "BOT"
    prior_history = []
    history_available = True

    if persisted_claim:
        customer_phone = persisted_claim["customer_phone"]
        conversation_id = persisted_claim["conversation_id"]
        state = persisted_claim["state"]
        prior_history = persisted_claim["history"]
        history_available = True

    # Capture the earlier conversation before saving this inbound event.  The
    # agent receives ``message_text`` separately, so this prevents the newest
    # customer turn from appearing twice in its context.
    if BOT_RESPONSE_MODE == "agent" and not persisted_claim:
        try:
            prior_history = load_history(customer_phone)
        except Exception as error:  # noqa: BLE001
            history_available = False
            print(f"ERROR cargando conversacion (tipo: {type(error).__name__})")

    if not persisted_claim:
        try:
            conversation_id, state, duplicate = record_inbound_message(
                customer_phone=customer_phone,
                body=message_text,
                wa_message_id=wa_message_id,
            )
            if duplicate:
                print("[Conversacion] Mensaje duplicado ignorado.")
                return JSONResponse(content={"ok": True})

            print(
                f"[Conversacion] Guardado en {conversation_id} "
                f"(estado: {state})."
            )
        except Exception as error:  # noqa: BLE001
            # Do not block the current template test if the history store is down.
            # Error details may contain database information, so log only its type.
            history_available = False
            print(f"ERROR guardando conversacion (tipo: {type(error).__name__})")

    if BOT_RESPONSE_MODE == "agent" and history_available and not persisted_claim:
        try:
            # A client often writes one thought in several bubbles. Wait a
            # small, bounded window; only the newest event in that burst gets
            # to decide and it receives the complete customer turn.
            if (
                not persisted_claim
                and CONVERSATION_DEBOUNCE_SECONDS
                and conversation_id
                and wa_message_id
            ):
                await asyncio.sleep(CONVERSATION_DEBOUNCE_SECONDS)
                if not is_latest_customer_message(conversation_id, wa_message_id):
                    print("[Conversacion] Mensaje agrupado en una ráfaga posterior.")
                    return JSONResponse(content={"ok": True})
                grouped_text = load_open_customer_turn(customer_phone)
                if grouped_text:
                    message_text = grouped_text
        except Exception as error:  # noqa: BLE001
            history_available = False
            print(f"ERROR agrupando conversacion (tipo: {type(error).__name__})")

    # ========================================================
    # RESPUESTA
    # ========================================================

    if BOT_RESPONSE_MODE == "agent":
        access_reply = _customer_access_reply(customer_phone)
        if access_reply:
            if send_whatsapp_text(customer_phone, access_reply) and conversation_id:
                record_bot_message(conversation_id, access_reply)
            print("[Operacion] Fred limitado por FRED_CUSTOMER_MODE={}.".format(FRED_CUSTOMER_MODE))
            return JSONResponse(content={"ok": True})

        # A database outage must not turn into a stateless AI conversation.
        if not history_available:
            _send_service_fallback(
                customer_phone, conversation_id, message_text, prior_history,
                "Fred no pudo acceder al historial de conversación.",
            )
            return JSONResponse(content={"ok": True})

        if state != "BOT":
            # Isa owns this thread. The cutoff is here, BEFORE any retrieval or
            # model call: Fred does not answer, does not think, and does not
            # compete with her -- he is only transport in this state.
            if state == "ISA":
                _relay_customer_message_to_isa(conversation_id, customer_phone, message_text)
            print(f"[Conversacion] El bot no responde en estado {state}.")
            return JSONResponse(content={"ok": True})

        # Fred Core: the persisted mode is the ONLY thing consulted to
        # decide whether this turn executes a deterministic action or goes
        # to CHAT. Nothing below this block may re-derive a competing
        # notion of "what flow are we in" from message text.
        core_state = get_fred_core_state(conversation_id)
        core_mode = core_state.get("mode") or "CHAT"
        if core_mode == "CHAT" and SALES_INTAKE_ENABLED:
            # Migration safety net (item 9): a conversation may carry a
            # real, active sales_intake from before Fred Core existed, or
            # from a residual path that doesn't set mode itself. Integrate
            # it as CHECKOUT rather than leave it untracked by mode or let
            # a second, competing flow start alongside it.
            try:
                legacy_intake = get_active_sales_intake(conversation_id)
            except Exception as error:  # noqa: BLE001
                legacy_intake = None
                print("ERROR leyendo ficha de venta heredada (tipo: {}).".format(type(error).__name__))
            if legacy_intake:
                core_mode = "CHECKOUT"
                save_fred_core_state(conversation_id, mode="CHECKOUT")
        print("[FredCore] mode={} active_product={} quantity={} checkout_step={}".format(
            core_mode, core_state.get("active_product_name"),
            core_state.get("quantity"), core_state.get("checkout_step"),
        ))
        if core_mode in ("CHECKOUT", "TRACKING"):
            try:
                flow_reply = _fred_core_dispatch(
                    core_mode, conversation_id, customer_phone, message_text, core_state, prior_history,
                )
            except Exception as error:  # noqa: BLE001
                print("ERROR en Fred Core (modo {}, tipo: {})".format(core_mode, type(error).__name__))
                _send_service_fallback(
                    customer_phone, conversation_id, message_text, prior_history,
                    "Fred no pudo continuar el flujo en curso.",
                )
                return JSONResponse(content={"ok": True})
            if flow_reply == "__HANDLED_NO_REPLY__":
                # _handle_sales_intake already sent and recorded the reply.
                return JSONResponse(content={"ok": True})
            if flow_reply is not None:
                _deliver_flow_reply(customer_phone, conversation_id, flow_reply)
                print("[FredCore] modo {} resuelto sin modelo.".format(core_mode))
                return JSONResponse(content={"ok": True})
            # None: CHECKOUT decided this message is about a different
            # product and released itself back to CHAT -- fall through and
            # reprocess this same message there.
            core_state = get_fred_core_state(conversation_id)

        # Fred no longer sells, so a [Comprar] tap can only come from a card
        # sent before this scope change. It must not open a checkout: the
        # customer is handed to Isa with the product they had chosen.
        if button_reply_id.startswith(BUY_BUTTON_PREFIX):
            sku = button_reply_id[len(BUY_BUTTON_PREFIX):].strip()
            print("[FredCore] botón Comprar heredado (SKU {}) -> Isa, sin checkout.".format(sku))
            reply = _isa_direct_contact_reply(_ISA_HANDOFF_LEADS["purchase_intent"])
            _deliver_flow_reply(customer_phone, conversation_id, reply)
            return JSONResponse(content={"ok": True})
        # Isa asked this customer something through Fred and the case is still
        # open: her answer belongs to that case, so it goes back to Isa instead
        # of being handled as an ordinary message. Fred still replies normally
        # to everything else -- only the answer to the open question is routed.
        _forward_customer_answer_to_isa(conversation_id, customer_phone, message_text)

        # A confirmation answers the question Fred actually asked. This runs
        # before any CHAT interpretation so a bare "sí"/"dale" can never be
        # re-read as a brand new message and sent back to product discovery
        # (the real production bug: "¿avanzamos?" -> "sí" -> "Encontré
        # SHOOW TOOLS - TAYLOR (CHOCOLATE)...").
        pending_intent = core_state.get("pending_intent")
        if pending_intent == PENDING_CONFIRM_PURCHASE_DRAFT and (
            _reads_as_affirmation(message_text) or _reads_as_negation(message_text)
        ):
            pending_draft = get_active_sales_intake(conversation_id)
            if pending_draft:
                _remember_pending_intent(conversation_id, None)
                if _handle_sales_intake(
                    conversation_id, customer_phone, message_text, pending_draft, prior_history,
                ):
                    print("[FredCore] confirmación resuelta contra la intención pendiente.")
                    return JSONResponse(content={"ok": True})
            else:
                # The draft is gone (already confirmed/cancelled elsewhere);
                # the stale intent must not keep intercepting messages.
                _remember_pending_intent(conversation_id, None)

        if _needs_purchase_clarification(message_text, prior_history):
            customer_reply = (
                "Para no confundirme: el set sorpresa lo dejamos descartado. "
                "¿Querés que busquemos otra opción natural o había otro modelo "
                "puntual con el que querías avanzar? 😊"
            )
            if send_whatsapp_text(customer_phone, customer_reply):
                record_bot_message(conversation_id, customer_reply)
            return JSONResponse(content={"ok": True})

        simple_reply = _simple_customer_reply(message_text)
        if simple_reply:
            if send_whatsapp_text(customer_phone, simple_reply):
                record_bot_message(conversation_id, simple_reply)
            print("[IA] Mensaje social resuelto sin modelo.")
            return JSONResponse(content={"ok": True})

        # CHAT: cheap, zero-LLM-round deterministic pre-checks on THIS
        # message only (never on Fred's own prior text) before spending a
        # model call -- direct requests for Isa, and unambiguous tracking.
        escalation_type = _customer_escalation_type(
            message_text,
            has_bot_history=any(
                message.get("role") == "assistant"
                for message in prior_history
            ),
        )
        if escalation_type == "human_handoff":
            reply = _isa_direct_contact_reply("Dale, lo mejor es que lo veas con Isa.")
            _deliver_flow_reply(customer_phone, conversation_id, reply)
            print("[Fred] Contacto de Isa entregado (sin crear caso).")
            return JSONResponse(content={"ok": True})

        order_number = extract_order_number(message_text)
        normalized_for_tracking = _knowledge_normalise(message_text)
        # "pedido 6345" names an order outright. The number IS the identifier,
        # so nothing has to be inferred from surrounding words or from history
        # -- and no catalog or product lookup can add anything to it. This
        # stands on its own even when Fred did not just ask for a number.
        strong_tracking_evidence = bool(
            _STRONG_TRACKING_TRIGGER_RE.search(normalized_for_tracking)
            or order_number_reference(normalized_for_tracking)
            or _pickup_of_a_specific_order(normalized_for_tracking)
        )
        if order_number and strong_tracking_evidence:
            reply = _fred_core_lookup_order(
                conversation_id, customer_phone, order_number, prior_history,
                pickup_requested=_pickup_requested(message_text, prior_history),
            )
            _deliver_flow_reply(customer_phone, conversation_id, reply)
            return JSONResponse(content={"ok": True})
        if strong_tracking_evidence and not order_number:
            save_fred_core_state(conversation_id, mode="TRACKING")
            _deliver_flow_reply(customer_phone, conversation_id, ORDER_NUMBER_PROMPT_TEXT)
            return JSONResponse(content={"ok": True})

        # Fred asked for an order number and this message is one. That answers
        # the question that was actually asked, so it is looked up directly --
        # no retrieval, no catalog search, and above all no rebuilding of
        # "what did the customer mean" from surrounding history, which is what
        # dragged an unrelated damaged-product complaint into an order lookup.
        # This catches the case the TRACKING mode above misses: the model
        # asking for the number in its own words instead of the fixed prompt.
        bare_order_number = _extract_bare_order_number(message_text)
        if bare_order_number and _fred_just_asked_for_order_number(prior_history):
            print("[FredCore] número de orden en respuesta directa: {}.".format(
                bare_order_number,
            ))
            reply = _fred_core_lookup_order(
                conversation_id, customer_phone, bare_order_number, prior_history,
                pickup_requested=_pickup_requested(message_text, prior_history),
            )
            _deliver_flow_reply(customer_phone, conversation_id, reply)
            return JSONResponse(content={"ok": True})

        # Fred does not advise and does not sell. Both are decided here, from
        # the customer's own words alone -- before any retrieval, any catalog
        # search, any live call and any model round. Doing it later would mean
        # paying for a product search whose only possible output is a
        # recommendation Isa does not want Fred to make.
        scope_reply = _isa_scope_handoff(message_text, core_state)
        if scope_reply:
            _deliver_flow_reply(customer_phone, conversation_id, scope_reply)
            return JSONResponse(content={"ok": True})

        # Observability starts here, after deterministic shortcuts and before
        # retrieval/model work. It intentionally measures only agent turns.
        agent_turn_started = time.monotonic()
        turn_timings = _new_turn_timings()
        # Per-turn, never module state: concurrent webhooks must not share a
        # counter. Defaults chosen so an early failure still logs honestly.
        turn_live_calls = {"count": 0}
        routing_requirement = {
            "intent": "unknown", "data_required": "catalog", "reason": "not_classified",
        }
        knowledge_health = {
            "embedding_status": "skipped", "retrieval_hits": 0, "embedding_error": "",
        }
        catalog_context = ""
        knowledge_context = ""
        knowledge_bundle = KnowledgeRetrieval()
        dynamic_check_outcomes = ()
        knowledge_answer_used = False
        policy_bypass = False
        result = {}
        try:
            # With Knowledge RAG off, preserve the established catalog path.
            # When it is enabled, both retrievers share one embedding rather
            # than paying for two calls. Retrieval is optional: an embedding
            # outage must not stop a normal agent reply.
            catalog_query = _catalog_retrieval_query(
                message_text, prior_history, core_state.get("active_product_name") or "",
            )
            if not KNOWLEDGE_RAG_ENABLED:
                with _timed(turn_timings, "catalog_ms"):
                    catalog_context = search_similar_products(catalog_query)
                knowledge_context = ""
            else:
                # The embedding is generated once and serves BOTH retrievers.
                # It is attributed to knowledge_ms because it only exists on
                # the Knowledge path (with Knowledge off, the catalog does its
                # own lexical search and no embedding is produced at all).
                with _timed(turn_timings, "knowledge_ms"):
                    try:
                        query_embedding = embed_text(
                            catalog_query, task_type="RETRIEVAL_QUERY"
                        )
                        knowledge_health["embedding_status"] = (
                            "ok" if query_embedding is not None else "empty"
                        )
                    except Exception as error:  # noqa: BLE001
                        print("ERROR generando embedding (tipo: {})".format(type(error).__name__))
                        query_embedding = None
                        # Recorded, not just printed: this is the difference
                        # between "the KB has no chunk" and "Knowledge never
                        # ran", which otherwise look identical downstream.
                        knowledge_health["embedding_status"] = "failed"
                        knowledge_health["embedding_error"] = type(error).__name__

                catalog_context = ""
                knowledge_context = ""
                if query_embedding is not None:
                    # Knowledge runs BEFORE the catalog now. Only once the
                    # approved answer is in hand can this turn be recognised as
                    # one the catalog cannot contribute to, and the catalog
                    # search skipped instead of paid for.
                    def retrieve_knowledge(retrieval_query):
                        return search_knowledge_bundle(
                            retrieval_query,
                            query_embedding=(
                                query_embedding
                                if retrieval_query == message_text
                                and catalog_query == message_text
                                else None
                            ),
                        )

                    with _timed(turn_timings, "knowledge_ms"):
                        knowledge_bundle, knowledge_query, _ = retrieve_with_recent_context(
                            message_text, prior_history, retrieve_knowledge
                        )
                    # Grounding observability: enough to tell from the logs
                    # whether retrieval found the approved answer, without
                    # dumping chunk contents.
                    knowledge_sections = [
                        line.split(" / ", 1)[1].split("]", 1)[0]
                        for line in (knowledge_bundle.context or "").splitlines()
                        if line.startswith("- [") and " / " in line
                    ]
                    print("[Knowledge] query={!r} topic={} hits={} sources={}".format(
                        knowledge_query[:60], knowledge_bundle.governing_topic,
                        len(knowledge_sections), knowledge_sections[:4],
                    ))
                    knowledge_health["retrieval_hits"] = len(knowledge_sections)
                    # Real Tiendanube calls (get_order_status/get_stock), not
                    # retrieval -- they belong to the live bucket even though
                    # Knowledge is what asked for them.
                    with _timed(turn_timings, "live_stock_ms"):
                        for _ in knowledge_bundle.dynamic_requirements or ():
                            _count_live_call(turn_live_calls)
                        dynamic_check_outcomes = execute_dynamic_requirements(
                            knowledge_bundle.dynamic_requirements,
                            {
                                "get_order_status": get_order_status,
                                "get_stock": get_stock,
                            },
                        )
                    dynamic_context = format_dynamic_check_context(
                        dynamic_check_outcomes
                    )
                    # Verified live facts (e.g. a real get_order_status/get_stock
                    # result) go first: build_turn_messages bounds rag_context
                    # to MAX_RAG_CONTEXT_CHARS by truncating the tail, and the
                    # one thing that must never be the part that gets cut is
                    # data a real tool already confirmed -- static approved
                    # prose can afford to be the part trimmed instead.
                    knowledge_context = "\n\n".join(
                        item for item in (dynamic_context, knowledge_bundle.context) if item
                    )

                    # The one bypass, and deliberately the narrowest possible
                    # one: an approved topic governs the turn AND the message
                    # names no product, no price, no stock, no quantity and no
                    # specific order (that is exactly what policy_question +
                    # knowledge_only means -- every commercial branch is
                    # checked before it). Such a turn cannot be improved by the
                    # catalog or the store, so neither is consulted.
                    #
                    # This is NOT "skip work whenever data_required is
                    # knowledge_only": advice is knowledge_only too and never
                    # reaches here, and any turn carrying a commercial object
                    # classifies as something else and keeps every lookup.
                    routing_requirement = classify_turn_data_requirement(
                        message_text,
                        governing_topic=knowledge_bundle.governing_topic,
                        knowledge_context=knowledge_context,
                        dynamic_requirements=knowledge_bundle.dynamic_requirements,
                        product_lexicon=product_lexicon(),
                        product_lexicon_available=product_lexicon_available(),
                    )
                    policy_bypass = (
                        routing_requirement["intent"] == INTENT_POLICY_QUESTION
                        and routing_requirement["data_required"] == DATA_KNOWLEDGE_ONLY
                        and bool(knowledge_bundle.governing_topic)
                    )
                    if policy_bypass:
                        print("[FredRouting] bypass: topic={} sin catálogo ni Tiendanube.".format(
                            knowledge_bundle.governing_topic,
                        ))
                    else:
                        with _timed(turn_timings, "catalog_ms"):
                            catalog_context = search_similar_products(
                                catalog_query, query_embedding=query_embedding
                            )
                else:
                    # Knowledge is optional. A temporary embedding/provider
                    # outage must still leave Fred with lexical retrieval and
                    # live Tiendanube verification for commercial answers.
                    with _timed(turn_timings, "catalog_ms"):
                        catalog_context = search_similar_products(catalog_query)

            # Classified above when Knowledge ran. With Knowledge off or the
            # embedding down there is no governing topic, so this classifies
            # the turn for the logs and can never reach the bypass.
            if routing_requirement.get("reason") == "not_classified":
                routing_requirement = classify_turn_data_requirement(
                    message_text,
                    governing_topic=knowledge_bundle.governing_topic,
                    knowledge_context=knowledge_context,
                    dynamic_requirements=knowledge_bundle.dynamic_requirements,
                    product_lexicon=product_lexicon(),
                    product_lexicon_available=product_lexicon_available(),
                )

            # Enriching the query with the active product keeps bare
            # follow-ups ("¿cómo quedan?") on topic, but it must never hide a
            # DIFFERENT product the customer just named: searching
            # "ISABEL I ... quiero 4 Taylor" returns Isabel I and Taylor never
            # becomes a candidate, so the checkout would open on the wrong
            # product. Search the customer's own words too and merge, then let
            # _live_product_candidate pick whichever one the message actually
            # names (it requires the name to be in the message).
            merged_own_words = False
            if not policy_bypass and catalog_query != message_text:
                try:
                    with _timed(turn_timings, "catalog_ms"):
                        own_words_context = search_similar_products(message_text)
                except Exception as error:  # noqa: BLE001
                    own_words_context = ""
                    print("ERROR buscando catálogo por el mensaje propio (tipo: {}).".format(
                        type(error).__name__
                    ))
                if own_words_context:
                    catalog_context = "\n".join(
                        part for part in (catalog_context, own_words_context) if part
                    )
                    merged_own_words = True

            # With two merged searches the newly-named product can sit past
            # the usual cutoff, so allow a few more live verifications on that
            # turn rather than silently dropping it.
            # Up to `limit` sequential get_product_availability calls against
            # Tiendanube, on every agent turn, before the model is ever asked
            # anything. Measuring it is the whole point of live_stock_ms.
            live_candidate_context = ""
            if not policy_bypass:
                with _timed(turn_timings, "live_stock_ms"):
                    live_candidate_context = _live_candidate_context(
                        catalog_context, catalog_query, limit=6 if merged_own_words else 3,
                        live_calls=turn_live_calls,
                    )
            if live_candidate_context:
                catalog_context = "{}\n\n{}".format(catalog_context, live_candidate_context)
            active_product_fact = ""
            if core_state.get("active_product_name"):
                # A plain fact for the model's own reasoning, on top of the
                # retrieval-query enrichment above: this is the product this
                # conversation is already about, so a follow-up that doesn't
                # repeat its name ("¿cómo quedan?") still has context instead
                # of restarting discovery from scratch.
                active_product_fact = "Producto activo de esta conversación: {}.".format(
                    core_state["active_product_name"]
                )
            checkout_pause_fact = ""
            if core_state.get("mode") == "CHECKOUT":
                # This message interrupted an in-progress checkout with a
                # real question (Fred Core left the checkout untouched so it
                # resumes on the next message) -- answer the question, then
                # naturally invite continuing instead of leaving it hanging.
                paused_intake = get_active_sales_intake(conversation_id)
                if paused_intake:
                    if not paused_intake.get("quantity"):
                        missing_desc = "decirte cuántas unidades quiere"
                    elif not paused_intake.get("fulfillment"):
                        missing_desc = "confirmar envío o retiro"
                    elif not paused_intake.get("customer_name") or not paused_intake.get("customer_email"):
                        missing_desc = "pasarte nombre y email"
                    else:
                        missing_desc = "confirmar el resumen de la compra"
                    checkout_pause_fact = (
                        "Hay una compra en curso pausada, esperando que la clienta {}. "
                        "Respondé primero su pregunta y después invitala con naturalidad "
                        "a seguir con la compra cuando quiera; no le pidas de nuevo un "
                        "dato que ya te dio."
                    ).format(missing_desc)
            rag_context = "\n\n".join(
                context for context in (
                    # Approved Knowledge before the catalog block: it is the
                    # smaller of the two and the one that must never be the
                    # part dropped if this ever gets truncated again.
                    active_product_fact, checkout_pause_fact, knowledge_context, catalog_context,
                ) if context
            )
            # A named product is the ONE active_product Fred Core knows about
            # going forward -- this write (not conversation_product_selections,
            # which older code paths may still touch but this orchestration no
            # longer reads) is what CHECKOUT/MENU anchor to later.
            selected_product_candidate = _live_product_candidate(
                live_candidate_context, message_text
            )
            if selected_product_candidate:
                try:
                    save_fred_core_state(
                        conversation_id, **_fred_core_active_product_fields(selected_product_candidate)
                    )
                except Exception as error:  # noqa: BLE001
                    # Active-product memory improves the journey but must never
                    # block a normal customer answer if storage is temporary.
                    print("ERROR guardando producto activo (tipo: {}).".format(type(error).__name__))
                core_state.update(_fred_core_active_product_fields(selected_product_candidate))

            # PURCHASE INTENT NO LONGER STARTS A CHECKOUT. Whatever the
            # customer wrote, this stays a conversation: the only way into
            # CHECKOUT is the [Comprar] button, whose id carries the real SKU.
            # What this block still does is RESOLVE identity, so that if the
            # customer does tap the button the product is already pinned to
            # something the live store confirmed.
            if _expresses_purchase(message_text) and not core_state.get("active_sku"):
                with _timed(turn_timings, "live_stock_ms"):
                    identity = _purchase_identity_from_message(
                        live_candidate_context, message_text,
                        core_state.get("active_product_name") or "",
                        live_calls=turn_live_calls,
                    )
                if identity["status"] == "resolved":
                    resolved = identity["candidate"]
                    try:
                        save_fred_core_state(
                            conversation_id, **_fred_core_active_product_fields(resolved)
                        )
                    except Exception as error:  # noqa: BLE001
                        print("ERROR guardando producto resuelto (tipo: {}).".format(
                            type(error).__name__,
                        ))
                    core_state.update(_fred_core_active_product_fields(resolved))
                    print("[FredCore] producto identificado para ofrecer compra: {}.".format(
                        resolved.get("product_name"),
                    ))
                elif identity["status"] == "ambiguous":
                    # The words are real and the catalog confirms them, but they
                    # name more than one sellable thing. Picking one here is the
                    # single most expensive mistake Fred can make (a wrong sale),
                    # so the code asks and the model never gets the chance to
                    # choose. Deterministic: no model call, no [Comprar] button,
                    # no active_sku written.
                    if (
                        not persisted_claim
                        and CONVERSATION_DEBOUNCE_SECONDS
                        and conversation_id
                        and wa_message_id
                        and not is_latest_customer_message(conversation_id, wa_message_id)
                    ):
                        print("[Conversacion] Respuesta obsoleta omitida por mensaje posterior.")
                        return JSONResponse(content={"ok": True})
                    question = _render_variant_question(identity)
                    print("[FredCore] compra ambigua: {} opciones reales, se pregunta.".format(
                        len(identity["options"]),
                    ))
                    if send_whatsapp_text(customer_phone, question):
                        record_bot_message(conversation_id, question)
                    # A real turn with a real cost: it did retrieval and live
                    # verification, it just never reached the model. Logging it
                    # is what keeps "Fred is slow" attributable.
                    _log_turn_timing(turn_timings, started_at=agent_turn_started)
                    _log_turn_knowledge(**knowledge_health)
                    _log_turn_routing(routing_requirement, live_calls=turn_live_calls)
                    _log_turn_decision(
                        topic=knowledge_bundle.governing_topic,
                        grounded_by="live",
                        core_state=core_state,
                        buttons_added=False,
                    )
                    return JSONResponse(content={"ok": True})

            # Disconnected, not deleted: this function's whole job is to
            # recommend a lash product, which is no longer Fred's to do. The
            # code and its tests stay while the new scope is validated in
            # production; reconnecting it is one constant.
            grounded_reply = (
                _grounded_lash_recommendation(live_candidate_context, catalog_query)
                if RECOMMENDATIONS_ENABLED else ""
            )
            if grounded_reply:
                # The live store already supplied exactly the facts this
                # recommendation needs. Avoid spending a model call and avoid
                # letting a generic search contradict those facts.
                result = {
                    "reply": grounded_reply,
                    "tool_calls": [],
                    "usage": {},
                    "model_calls": 0,
                }
            else:
                knowledge_answer_used = True
                with _timed(turn_timings, "llm_ms"):
                    result = answer(
                        message_text,
                        history=prior_history,
                        rag_context=rag_context,
                        greeting_required=not any(
                            message.get("role") == "assistant"
                            for message in prior_history
                        ),
                        verbose=False,
                    )
            usage = result.get("usage") or {}
            print(
                "[IA] llamadas={} prompt_tokens={} completion_tokens={}".format(
                    result.get("model_calls", 0),
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
            )
            reply = (result.get("reply") or "").strip()
            if not reply:
                raise RuntimeError("El agente no devolvió texto.")

            if result.get("graceful_fallback_tier") == "escalate":
                # The model could not close a confident answer. Present the
                # one universal fallback (menú de 4 opciones) instead of its
                # raw hedge text, and transition Fred Core into MENU so the
                # next reply (a bare "1"-"4") is resolved deterministically,
                # never by re-reading this message's text.
                if (
                    not persisted_claim
                    and CONVERSATION_DEBOUNCE_SECONDS
                    and conversation_id
                    and wa_message_id
                    and not is_latest_customer_message(conversation_id, wa_message_id)
                ):
                    print("[Conversacion] Respuesta obsoleta omitida por mensaje posterior.")
                    return JSONResponse(content={"ok": True})
                # No menu. When Fred genuinely cannot ground an answer he says
                # so briefly and offers the one action that helps: Isa.
                honest = _isa_direct_contact_reply(
                    "Ese dato no lo tengo confirmado y prefiero no inventártelo."
                )
                print("[Fred] grounded_by=none -> respuesta honesta + contacto de Isa.")
                if send_whatsapp_text(customer_phone, honest):
                    record_bot_message(conversation_id, honest)
                _log_turn_timing(
                    turn_timings,
                    started_at=agent_turn_started,
                    tool_calls=len(result.get("tool_calls") or ()),
                    tokens_input=usage.get("prompt_tokens", 0) or 0,
                    tokens_output=usage.get("completion_tokens", 0) or 0,
                )
                _log_turn_knowledge(**knowledge_health)
                _log_turn_routing(routing_requirement, live_calls=turn_live_calls)
                _log_turn_decision(
                    topic=knowledge_bundle.governing_topic,
                    grounded_by="none",
                    core_state=core_state,
                    buttons_added=False,
                )
                return JSONResponse(content={"ok": True})

            sale_candidate = result.get("sale_candidate")
            handoff = result.get("handoff")
            decision = result.get("decision") or {}
            print(
                "[Decision] action={} reason={}".format(
                    decision.get("action", "unknown"),
                    decision.get("reason", "unknown"),
                )
            )
            routing = resolve_harness_routing(
                message_text,
                prior_history,
                decision=decision,
                handoff=handoff,
                knowledge_retrieval=knowledge_bundle,
                dynamic_requirements=dynamic_check_outcomes,
            )
            decision = routing["decision"]
            handoff = routing["handoff"]
            reply = align_reply_with_routing(
                reply,
                routing,
                dynamic_requirements=dynamic_check_outcomes,
            )
            # Obligations are a final-output invariant. Routing may replace
            # model wording, so enforce the same approved disclosures/links
            # after that alignment to ensure none are accidentally lost.
            if knowledge_answer_used and knowledge_bundle.context:
                reply = enforce_knowledge_obligations(
                    reply,
                    knowledge_bundle.obligations,
                    verified_dynamic_links=extract_https_urls(catalog_context),
                )
            if handoff:
                sale_candidate = None
            # Un fallo de identificación recibe una sola repregunta. Si la
            # clienta ya respondió esa repregunta y aún no podemos verificar el
            # modelo, Isa recibe un caso realmente excepcional y con contexto.
            if (
                (result.get("needs_product_clarification") or decision.get("action") == "clarify_product")
                and not handoff
                and _already_asked_product_clarification(prior_history)
            ):
                handoff = {
                    "reason": "unable_to_verify",
                    "summary": (
                        "Fred pidió una precisión para identificar el producto, "
                        "pero todavía no pudo verificarlo en Tiendanube."
                    ),
                }
            # DeepSeek can sometimes explain a purchase correctly but omit the
            # select_sale_candidate call. Do not let that create a fake
            # text-only checkout flow: if it verified exactly one SKU in this
            # turn, promote it to the persisted intake form ourselves.
            if not sale_candidate and SALES_INTAKE_ENABLED:
                sale_candidate = _verified_purchase_candidate_from_tool_calls(
                    message_text,
                    result,
                )
            if sale_candidate and SALES_INTAKE_ENABLED:
                # The model may still identify the exact variant, and that is
                # useful -- but it may NOT open a checkout. Its selection only
                # pins the active product so the [Comprar] button carries a
                # real SKU; the customer still has to tap it.
                try:
                    save_fred_core_state(
                        conversation_id, **_fred_core_active_product_fields(sale_candidate)
                    )
                    core_state.update(_fred_core_active_product_fields(sale_candidate))
                    print("[FredCore] variante identificada por el modelo: {}.".format(
                        sale_candidate.get("product_name"),
                    ))
                except Exception as error:  # noqa: BLE001
                    print("ERROR guardando selección del modelo (tipo: {}).".format(
                        type(error).__name__,
                    ))
                handoff = None

            if handoff:
                if (
                    handoff.get("reason") == "special_sale_request"
                    or _is_special_sale_context(message_text, prior_history)
                ):
                    # An encargo/cotización can have no published SKU, price or
                    # stock yet. It must reach Isa as a consultation, never as
                    # a normal cart approval.
                    action_type = "special_sale_request"
                elif handoff.get("reason") == "purchase_intent":
                    action_type = "purchase_review"
                elif handoff.get("reason") == "human_request":
                    action_type = "human_handoff"
                else:
                    action_type = "bot_fallback"
                if action_type == "purchase_review" and SALES_INTAKE_ENABLED:
                    # A model may detect purchase intent, but it may never
                    # open a blank sale form. Only a SKU verified above can
                    # create an intake; otherwise ask for one clear choice.
                    reply = (
                        "Dale 😊 Para avanzar necesito saber cuál producto querés llevar. "
                        "Decime el modelo o mandame el link y lo verifico."
                    )
                else:
                    # Fred ya no abre casos para Isa por decisión del modelo:
                    # responde con honestidad y deja su contacto. El summary
                    # del handoff es material de auditoría para Isa y nunca
                    # texto para la clienta -- la frase sale del reason.
                    reply = _isa_direct_contact_reply(
                        _isa_handoff_lead(handoff.get("reason"))
                    )

            # C4 is presentation-only: decisions, tools and sales state are
            # already final. Deterministic sales-intake copy is left untouched.
            if knowledge_answer_used and not sale_candidate:
                final_routing = {"decision": decision, "handoff": handoff}
                reply = apply_conversation_contract(
                    reply,
                    history=prior_history,
                    routing_contract=visible_routing_contract(
                        final_routing,
                        dynamic_requirements=dynamic_check_outcomes,
                    ),
                )

            # A generic "I don't know" the specific mechanisms above didn't
            # already turn into a handoff or a grounded discovery answer:
            # offer the same universal menu instead of a bare hedge, so
            # "Fred no sabe" always ends in the same four concrete options.
            # A hedge is not a menu trigger any more: the reply stands, and
            # the Isa button is offered alongside it further below.
            reply_is_ungrounded = bool(
                not handoff and not sale_candidate and _looks_like_a_hedge(reply)
            )

            # A slower model/tool turn must never answer an earlier version of
            # the customer's thought. The newer inbound webhook will own the
            # reply instead.
            if (
                not persisted_claim
                and CONVERSATION_DEBOUNCE_SECONDS
                and conversation_id
                and wa_message_id
            ):
                if not is_latest_customer_message(conversation_id, wa_message_id):
                    print("[Conversacion] Respuesta obsoleta omitida por mensaje posterior.")
                    return JSONResponse(content={"ok": True})

            grounded_by = "none" if reply_is_ungrounded else "|".join(
                source for source, present in (
                    ("knowledge", bool(knowledge_context)),
                    ("catalog", bool(catalog_context)),
                    ("live", bool(dynamic_check_outcomes)),
                ) if present
            ) or "model"
            print("[Fred] grounded_by={}".format(grounded_by))

            delivered = False
            buttons_added = False
            if reply == "__FULFILLMENT_BUTTONS__":
                delivered = send_customer_fulfillment_buttons(customer_phone)
                if delivered:
                    record_bot_message(
                        conversation_id, "¿Cómo preferís recibir tu compra? [Envío / Retiro]",
                    )
                    print("[Conversacion] Respuesta del agente guardada.")
            elif core_state.get("mode") == "CHAT" and _offer_customer_actions(
                conversation_id, customer_phone,
                # Personalised advice is Isa's: Fred orients briefly and hands
                # over her contact in the same message, without opening a case.
                _isa_direct_contact_reply(
                    "{}\n\nPara una recomendación más personalizada,".format(reply)
                ) if _is_personalised_advice(message_text) else reply,
                core_state,
            ):
                # A normal CHAT answer carries the two explicit doors out of
                # the conversation. Offering them changes nothing by itself.
                delivered = True
                buttons_added = True
                print("[Conversacion] Respuesta del agente con botones de acción.")
            elif send_whatsapp_text(customer_phone, reply):
                delivered = True
                record_bot_message(conversation_id, reply)
                print("[Conversacion] Respuesta del agente guardada.")

            effective_action = (
                "handoff_to_isa" if handoff
                else "start_sales_intake" if sale_candidate
                else decision.get("action", "reply")
            )
            effective_reason = (
                handoff.get("reason") if handoff
                else decision.get("reason", "normal_response")
            )
            _record_agent_turn_safely(
                wa_message_id=wa_message_id,
                conversation_id=conversation_id,
                result=result,
                action=effective_action,
                reason=effective_reason,
                outcome="replied" if delivered else "send_failed",
                catalog_context_used=bool(catalog_context),
                knowledge_context_used=bool(knowledge_context),
                duration_ms=round((time.monotonic() - agent_turn_started) * 1000),
            )
            # llm_ms is the whole answer() call, so it also contains the tool
            # calls the agent makes inside its own rounds (which are Tiendanube
            # HTTP, not model time). tool_calls is what tells the two apart:
            # a large llm_ms with a high tool_calls is a tools problem, the
            # same number with tool_calls=0 is a model problem.
            _log_turn_timing(
                turn_timings,
                started_at=agent_turn_started,
                tool_calls=len(result.get("tool_calls") or ()),
                tokens_input=usage.get("prompt_tokens", 0) or 0,
                tokens_output=usage.get("completion_tokens", 0) or 0,
            )
            _log_turn_knowledge(**knowledge_health)
            _log_turn_routing(routing_requirement, live_calls=turn_live_calls)
            _log_turn_decision(
                topic=knowledge_bundle.governing_topic,
                grounded_by=grounded_by,
                core_state=core_state,
                buttons_added=buttons_added,
            )
        except Exception as error:  # noqa: BLE001
            print(f"ERROR respondiendo con agente (tipo: {type(error).__name__})")
            _send_service_fallback(
                customer_phone, conversation_id, message_text, prior_history,
                "Fred no pudo completar una respuesta verificada.",
            )
            _record_agent_turn_safely(
                wa_message_id=wa_message_id,
                conversation_id=conversation_id,
                result=result,
                action="service_fallback",
                reason="agent_error",
                outcome="service_fallback",
                catalog_context_used=bool(catalog_context),
                knowledge_context_used=bool(knowledge_context),
                duration_ms=round((time.monotonic() - agent_turn_started) * 1000),
            )
            # A failed turn is the one you most want timings for: it still
            # spent whatever it spent before falling over.
            _log_turn_timing(turn_timings, started_at=agent_turn_started)
            _log_turn_knowledge(**knowledge_health)
            _log_turn_routing(routing_requirement, live_calls=turn_live_calls)
            _log_turn_decision(
                topic=knowledge_bundle.governing_topic,
                grounded_by="error",
                core_state=core_state,
                buttons_added=False,
            )

        return JSONResponse(content={"ok": True})

    # Modo seguro por defecto: plantilla Meta escalacion_isa con {{1}} = 1.
    send_escalacion_isa_template(
        customer_phone,
        pending_inquiries=1,
    )

    return JSONResponse(
        content={"ok": True}
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health")
async def health():
    """Health check para Railway."""

    return {
        "status": "ok"
    }


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv("PORT", 8000)
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
    set_sales_intake_customer,
    set_sales_intake_fulfillment,
    set_sales_intake_product,
    set_sales_intake_quantity,
    start_sales_intake,
