"""Pure routing policy shared by production and the read-only shadow harness.

This module must stay free of databases, HTTP calls and side effects. The LLM
proposes; verified knowledge obligations and existing hard business boundaries
decide whether the turn can remain with Fred.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Mapping, Optional, Sequence


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9]+", " ", value.lower()).split())


def legacy_special_sale_context(message_text: str, prior_history: Sequence[Mapping[str, Any]]) -> bool:
    """Preserve the old safety boundary when reviewed knowledge is unavailable."""
    normalized_current = _normalise(message_text)
    special_words = r"\b(encargo|encargar|preventa|mayorista|cotizacion)\b"
    if re.search(special_words, normalized_current):
        return True
    last_assistant_text = next(
        (
            _normalise(item.get("content", ""))
            for item in reversed(prior_history or [])
            if item.get("role") == "assistant"
        ),
        "",
    )
    is_short_affirmation = bool(re.fullmatch(
        r"(?:si|si porfa|dale|ok|perfecto)", normalized_current.strip()
    ))
    return bool(is_short_affirmation and re.search(special_words, last_assistant_text))


def lifting_clarification_reply(text: str) -> str:
    """Current production-safe pre-route, exposed so shadow evaluates it too."""
    if "lifting" not in _normalise(text):
        return ""
    return (
        "Para orientarte bien sin prometerte algo que no esté confirmado: "
        "¿tenés el nombre exacto o el link del modelo que te gustaría usar? "
        "Si todavía no lo elegiste, contame si buscás pestañas de banda o cluster "
        "y te ayudo a ubicar opciones 😊"
    )


# --- what data does this turn actually need? --------------------------
#
# Fred pays for catalog retrieval AND live Tiendanube verification on every
# agent turn, including turns the approved Knowledge base answers on its own
# ("¿cuál es el horario?"). This names what a turn genuinely requires so the
# spending can be measured, and later cut, without guessing.
#
# Pure, like the rest of this module: it reads the message plus what retrieval
# already returned, and returns labels. It performs no lookups, answers
# nothing, and changes no routing -- the caller decides what to do with it.
#
# Design rule, learned from replaying real traffic: the ONLY safe direction to
# be wrong in is "spend more". A turn wrongly sent to the catalog costs
# milliseconds; a turn wrongly answered from a document while the customer was
# asking about their own order, a price, or a specific product is a wrong
# answer. So every blocker below is deliberately generous, and anything
# unrecognised falls through to spending.

# Intent labels. These name WHY a turn needs what it needs, which is the part
# that is auditable after a bad answer -- "data_required=live" alone never
# tells you whether Fred understood the question.
INTENT_KNOWLEDGE_REQUIRES_LIVE = "knowledge_requires_live"
INTENT_INDIVIDUAL_CLAIM = "individual_claim"
INTENT_EXISTING_ORDER = "existing_order"
INTENT_PRICE_REQUEST = "price_request"
INTENT_STOCK_REQUEST = "stock_request"
INTENT_PURCHASE_INTENT = "purchase_intent"
INTENT_ADVICE_REQUEST = "advice_request"
INTENT_PRODUCT_NAMED = "product_named"
INTENT_VARIANT_ATTRIBUTE = "variant_attribute"
INTENT_ANAPHORIC_REFERENCE = "anaphoric_reference"
INTENT_PRODUCT_INTEREST = "product_interest"
INTENT_POLICY_QUESTION = "policy_question"
INTENT_UNKNOWN = "unknown"

DATA_KNOWLEDGE_ONLY = "knowledge_only"
DATA_CATALOG = "catalog"
DATA_LIVE = "live"

# A complaint about something that already happened to THIS customer. The
# first-person marker is what separates it from the approved policy question:
# "¿cómo funcionan las devoluciones?" is Knowledge, "quiero devolver lo que me
# llegó roto" is a case about one order and one product.
_INDIVIDUAL_CLAIM_RE = re.compile(
    r"\b(reclamo|reclamar|devolver|devoluci[óo]n|cambiar|cambio|garantia|"
    r"equivocad\w+|fallad\w+|defectuos\w+|rot[oa]s?|dañad\w+|"
    r"no funciona|mal estado|vino mal|lleg[óo] mal|me mandaron)\b"
)
_FIRST_PERSON_RE = re.compile(r"\b(mi|mis|me|yo|conmigo|mio|mia)\b")

# --- collecting an order: policy question vs one real order ------------
#
# "¿cómo puedo retirar un pedido?" and "quiero retirar mi pedido" share a verb
# and a noun and mean completely different things. The first asks how pickups
# work -- approved Knowledge answers it and asking for an order number is a
# non sequitur. The second is about ONE order that exists, which cannot be
# answered without looking it up.
#
# Keying on "retirar + pedido" could not tell them apart, and sent a policy
# question into TRACKING. The distinction is grammatical, not lexical: a
# question about the PROCEDURE versus a reference to a SPECIFIC order.
_PICKUP_OF_AN_ORDER_RE = re.compile(
    r"retir\w+\s+(?:un|una|mi|mis|el|la|los|las)?\s*(?:pedido|orden|compra|paquete)|"
    r"pas(?:o|ar)\s+a\s+(?:buscar|retirar)"
)
# Asking how something is done, or what it requires. Generic by nature: a
# procedure has no order number.
_PROCEDURE_QUESTION_RE = re.compile(
    r"\bcomo\b|\bde\s+que\s+manera\b|\bque\s+necesito\b|\bque\s+hace\s+falta\b|"
    r"\bque\s+requisitos\b|\bcuales\s+son\s+los\s+requisitos\b|"
    r"\bque\s+tengo\s+que\s+hacer\b|\bse\s+puede\b|\bes\s+necesario\b|"
    r"\bcomo\s+funciona\b"
)
# An order named by its number: "pedido 6345", "orden #6345". This is the
# least ambiguous thing a customer can say about an order -- the number IS the
# identifier -- so it needs no surrounding evidence and no retrieval to work
# out what it means. It stands alone even when Fred did not just ask for it.
_ORDER_NUMBER_REFERENCE_RE = re.compile(r"\b(?:pedido|orden)\s*#?\s*(\d{2,})\b")


def order_number_reference(normalized_message: str) -> str:
    """The order number a message names outright, or ""."""
    match = _ORDER_NUMBER_REFERENCE_RE.search(normalized_message)
    return match.group(1) if match else ""


# Pointing at ONE order that already exists: possessive, or identified by
# number, or explicitly the one they placed.
_SPECIFIC_ORDER_REF_RE = re.compile(
    r"\bmis?\s+(?:pedido|orden|compra|paquete)\b|"
    r"\b(?:el|la)\s+(?:pedido|orden|compra)\s+que\s+(?:hice|compre|pedi|encargue)\b|"
    r"\b(?:pedido|orden)\s*#?\s*\d{2,}\b"
)


def _pickup_of_a_specific_order(normalized_message: str) -> bool:
    """Does this ask to collect ONE existing order (-> look it up), rather
    than asking how pickups work (-> approved policy answers it)?

    A specific order reference wins outright: "¿cómo retiro el pedido 6295?"
    names an order, so its real state still matters. Otherwise a procedural
    question means this is policy, and no order number should ever be asked
    for -- which is the regression this exists to prevent.
    """
    if not _PICKUP_OF_AN_ORDER_RE.search(normalized_message):
        return False
    if _SPECIFIC_ORDER_REF_RE.search(normalized_message):
        return True
    return not _PROCEDURE_QUESTION_RE.search(normalized_message)


# An order that already exists. No document can report its state.
_EXISTING_ORDER_RE = re.compile(
    r"\b(mi pedido|el pedido|mis pedidos|mi orden|mi compra|"
    # "no supe más nada DEL envío" is the same question as "mi envío".
    r"(?:mi|el|del)\s+env[íi]o|"
    r"mi paquete|numero de orden|n[úu]mero de orden|seguimiento|tracking|"
    r"hice un pedido|ya compr[ée]|habia comprado|hab[íi]a comprado|"
    r"despach\w+|sucursal|correo argentino|andreani|"
    r"(?:ya\s+)?lleg[óo]|llegaron|no me lleg\w*|sigue en pie|"
    r"pedido pendiente|pedido abonado)\b"
)

# Commercial facts only the live store can answer truthfully.
_PRICE_REQUEST_RE = re.compile(
    r"\b(precio[s]?|valor|cotizaci[óo]n|cotizar|presupuesto|"
    # "¿a cuánto están?" and "¿qué sale?" are the everyday phrasings and carry
    # no other price word at all -- requiring "cuánto SALE" missed both.
    # "cuánto <algo>" is a price question EXCEPT when the something is stock:
    # "¿cuánto stock hay?" asks about availability, and mislabelling it as a
    # price request would make the logs lie about what Fred understood.
    r"a\s+cuanto|cuanto\s+(?!stock|disponib|queda|hay)\w+|que\s+sale[n]?|abonar|"
    r"a\s+que\s+precio|en\s+cuanto|el\s+total|descuento[s]?|promo|promoci[óo]n|"
    r"lista\s+de\s+precios|minimo\s+de\s+compra)\b"
)
# Possession verbs are availability questions ("¿tenés Isabel I?" means "is it
# in stock right now?"), which is how agent._availability_requested already
# reads them -- one vocabulary across the codebase, not two competing ones.
# "¿dónde queda el showroom?" is a location question, not a stock question --
# the lookbehind is what keeps the most common Knowledge-only phrasing in the
# store from being dragged into a live check.
_STOCK_REQUEST_RE = re.compile(
    r"\b(stock|disponible[s]?|disponibilidad|agotad\w+|(?<!donde )queda[n]?|hay|"
    r"tienen|tenes|tendran|tenian|entrar[áa]n|entran|repon\w+|reposici[óo]n|"
    r"vuelve[n]?\s+a\s+entrar)\b"
)
# Deciding to buy. A purchase can never be pinned without live stock, so this
# forces live rather than merely catalog.
_PURCHASE_INTENT_RE = re.compile(
    r"\b(comprar|compro|compra[r]?me|encargar|encargo|te\s+pido|le\s+pido|"
    r"me\s+llevo|lo\s+llevo|las?\s+llevo|los\s+llevo|llevar|llevo|"
    r"me\s+quedo\s+con|me\s+interesa[n]?|reserv\w+|apart\w+|"
    # "necesito 2 packs" is a purchase. "qué necesito para retirar" asks what
    # a procedure requires, which approved policy answers -- the interrogative
    # in front is the whole difference.
    r"quiero|quisiera|(?<!que )necesito|dame|mandame|pagar[íi]a|transferencia|"
    r"proceder|avanzar|hacer\s+el\s+pedido)\b"
)
# Asking WHICH ONE to get. Fred does not advise: choosing between products for
# a person is Isa's job, so this is a handoff rather than a search. Recognising
# it early is also what stops a discovery loop that only ever existed to
# produce a recommendation nobody wants Fred to make.
_ADVICE_REQUEST_RE = re.compile(
    r"\b(recomend\w+|recomiend\w+|suger\w+|asesor\w+|"
    r"me\s+convien\w+|cual\s+me\s+(?:convien\w+|sirve|queda|va|ayuda)|"
    r"cual\s+es\s+mejor|cual\s+seria\s+mejor|que\s+me\s+(?:convien\w+|queda|sirve)|"
    r"no\s+se\s+cual\w*|no\s+se\s+que\s+(?:llevar|elegir|comprar)|"
    r"ayuda\w*\s+a\s+elegir|me\s+ayudas\s+a\s+elegir|"
    r"que\s+tono\s+me|para\s+ojos\s+\w+|para\s+lifting|"
    r"cual\w*\s+son\s+mejor\w*|parecid\w+\s+a)\b"
)
# Describing a need instead of naming a product ("unas pestañas naturales").
# Only counts as advice when no concrete product was named -- see below.
_VAGUE_PRODUCT_SEARCH_RE = re.compile(
    r"\b(busco|buscaba|buscando|estoy\s+buscando|quiero\s+(?:un|una|unos|unas)|"
    r"necesito\s+(?:un|una|unos|unas)|algo\s+(?:para|que|natural|lindo|similar))\b"
)


# A variant axis: the customer is choosing WITHIN a product, which can only be
# resolved against the real catalog.
_VARIANT_ATTRIBUTE_RE = re.compile(
    r"\b(color(?:es)?|tono[s]?|tama[ñn]o[s]?|talle[s]?|medida[s]?|largo[s]?|"
    r"\d+\s*mm|\d+mm|variante[s]?|modelo[s]?|version|presentaci[óo]n|"
    r"docena[s]?|pack[s]?|unidad(?:es)?|par(?:es)?|caja[s]?|"
    # A colour is an attribute of a product, never a product name. Catalog
    # names contain them, so they are excluded from the product lexicon and
    # recognised here instead -- same blocking effect, honest label.
    r"negro|negra|blanco|blanca|rojo|roja|verde|azul|rosa|rosado|marron|"
    r"chocolate|dorado|plateado|nude|transparente)\b"
)
# Pointing at something said earlier. The message carries no identity of its
# own, so it can only be resolved with the conversation's product context --
# never from a policy document. This is the category that produced the worst
# production failure: "Las quiero" answered with silver hair flowers.
_ANAPHORIC_REFERENCE_RE = re.compile(
    r"\b(la[s]?\s+dos|lo[s]?\s+dos|las\s+tres|ese\s+producto|esa\s+opci[óo]n|"
    r"el\s+anterior|la\s+anterior|el\s+mismo|la\s+misma|los\s+otros|"
    r"las\s+otras|la\s+otra|el\s+otro|la\s+primera|el\s+primero|"
    r"la\s+segunda|la\s+[úu]ltima|de\s+esos|de\s+esas|"
    r"esos|esas|estos|estas|aquell\w+)\b"
)
# Wanting a product without naming one.
_PRODUCT_INTEREST_RE = re.compile(
    r"\b(busco|buscaba|buscando|recomend\w+|suger\w+|producto[s]?|"
    r"cat[áa]logo|opciones|alternativa[s]?)\b"
)

# Catalog words that identify nothing on their own: house brands, category
# nouns, packaging units and Spanish function words. Everything else in a real
# product name is treated as identifying.
_NON_IDENTIFYING_CATALOG_WORDS = frozenset({
    "shoow", "tools", "beauty", "house", "studio", "cosmetics", "creations",
    "deluxe", "italia", "makeup", "make", "professional", "profesional",
    "mayorista", "unid", "unidades", "pack", "packs", "caja", "cajas", "set",
    "sets", "kit", "kits", "pares", "pair", "pairs", "pcs", "unidad",
    "pestanas", "pestana", "lash", "lashes", "para", "con", "sin", "por",
    "del", "las", "los", "una", "uno", "the", "and", "color", "colores",
    "tono", "tonos", "venta", "preventa", "nuevo", "nueva", "edicion",
    "sorpresa", "combo", "promo", "oferta", "linea", "serie",
    # Ordinary Spanish that happens to appear inside product names. Left in,
    # these silently block approved policy answers: one catalog item containing
    # "TODO" was enough to send "¿hacen envíos a todo el país?" to the catalog.
    "todo", "toda", "todos", "todas", "solo", "sola", "bajo", "tipo", "tipos",
    "grande", "chico", "chica", "largo", "corto", "mejor", "gratis", "natural",
    "mini", "maxi", "super", "ultra", "full", "plus", "basic", "clasico",
    "mano", "manos", "casa", "parte", "desde", "hasta", "entre", "sobre",
    # Logistics, not identity. Catalog entries like "PEDIDO AL MAYOR" and
    # "(BAJO PEDIDO) PROSA - ..." put "pedido" in the lexicon, which then made
    # "¿cómo puedo retirar un pedido?" look like a named product.
    "pedido", "pedidos", "orden", "ordenes", "compra", "compras", "envio",
    "envios", "retiro", "retiros", "entrega", "entregas", "mayor",
    # Colours are variant attributes, handled by _VARIANT_ATTRIBUTE_RE.
    "negro", "negra", "blanco", "blanca", "rojo", "roja", "verde", "azul",
    "rosa", "rosado", "marron", "chocolate", "dorado", "plateado", "nude",
})


# Category nouns are excluded from the lexicon (they identify no single
# product), but naming a CATEGORY is still naming a product -- "¿y pegamento
# de pestañas?" is a catalog question, not a policy one. Checked separately so
# the two stay distinguishable in the logs.
_PRODUCT_CATEGORY_RE = re.compile(
    r"\b(pesta[ñn]a[s]?|pega|pegamento|adhesivo|removedor|rizador|pinza[s]?|"
    r"aplicador|cluster[s]?|banda[s]?|rimel|mascara\s+de\s+pesta[ñn]as|"
    r"labial|sombra[s]?|corrector|delineador|brocha[s]?|base|iluminador|"
    r"rubor|blush|primer|polvo|kit\s+de\s+retoque)\b"
)


def build_product_lexicon(product_names: Sequence[str]) -> frozenset:
    """Identifying words drawn from REAL catalog product names.

    The catalog defines what a product is called; this only decides which of
    those words could single one out. Generic brand/category/packaging words
    are dropped because they name no product ("beauty" matches ten unrelated
    things), and everything else is kept regardless of how many products share
    it -- "foxy" spans 42 products and is exactly the kind of word that must
    still block a Knowledge-only answer.
    """
    lexicon = set()
    for name in product_names or ():
        for word in _normalise(name).split():
            if len(word) < 4 or word.isdigit():
                continue
            if word in _NON_IDENTIFYING_CATALOG_WORDS:
                continue
            lexicon.add(word)
    return frozenset(lexicon)


def _named_catalog_product(normalised_message: str, product_lexicon) -> str:
    """The first catalog word the customer actually used, or "". Whole-word
    only: a substring hit inside another word is a coincidence, not a name."""
    if not product_lexicon:
        return ""
    for word in normalised_message.split():
        if word in product_lexicon:
            return word
    return ""


# A commercial signal needs a commercial OBJECT. "quiero" and "tienen" are the
# cheapest words in the language: on their own they turned "quiero pasar por el
# showroom" into purchase_intent and "qué horarios tienen" into a stock
# request, and both went off to the catalog and the store for a question the
# approved policy answers on its own.
#
# These are the signals that genuinely mean a turn is about merchandise or one
# order, as opposed to about how the business works.
_EXPLICIT_PRICE_WORD_RE = re.compile(
    r"\b(precio[s]?|cotizaci[óo]n|presupuesto|cuanto\s+(?:sale|cuesta|vale)|"
    r"descuento|lista\s+de\s+precios)\b"
)
_EXPLICIT_STOCK_WORD_RE = re.compile(
    r"\b(stock|disponible[s]?|disponibilidad|agotad\w+|hay\s+de|queda[n]?\s+\w)\b"
)
_QUANTITY_OF_SOMETHING_RE = re.compile(r"\b\d+\s+\w{3,}")
# Verbs that mean "buy" and nothing else. Unlike "quiero" or "tienen", these
# cannot appear in a policy question, so they are commercial signals on their
# own -- "quiero comprar dos" must never be answered from a document.
_UNAMBIGUOUS_PURCHASE_VERB_RE = re.compile(
    r"\b(comprar|compro|encargar|encargo|me\s+llevo|lo\s+llevo|las?\s+llevo|"
    r"reservar|apartar)\b"
)


# Wholesale conditions are approved content, not live data: the list, the
# minimums and the prices come from Isa, and the store has nothing to add to
# them. A wholesale question that only asks for information is therefore a
# policy question even when it says "cuánto salen" -- asking Tiendanube for a
# retail price would answer a different question.
_WHOLESALE_QUESTION_RE = re.compile(r"\b(mayorista[s]?|por\s+mayor|reventa|mayoreo)\b")


def _wholesale_information_request(normalized_message: str) -> bool:
    """Asking ABOUT wholesale, as opposed to placing a wholesale order."""
    if not _WHOLESALE_QUESTION_RE.search(normalized_message):
        return False
    return not _UNAMBIGUOUS_PURCHASE_VERB_RE.search(normalized_message)


def _carries_commercial_object(normalized_message: str, product_lexicon: Any) -> bool:
    """Is this turn about merchandise or one specific order, rather than about
    how the business operates?"""
    return bool(
        _named_catalog_product(normalized_message, product_lexicon)
        or _PRODUCT_CATEGORY_RE.search(normalized_message)
        or _EXPLICIT_PRICE_WORD_RE.search(normalized_message)
        or _EXPLICIT_STOCK_WORD_RE.search(normalized_message)
        or _SPECIFIC_ORDER_REF_RE.search(normalized_message)
        or _QUANTITY_OF_SOMETHING_RE.search(normalized_message)
        or _UNAMBIGUOUS_PURCHASE_VERB_RE.search(normalized_message)
        # Pointing at something shown earlier is pointing at merchandise.
        or _ANAPHORIC_REFERENCE_RE.search(normalized_message)
    )


def classify_turn_data_requirement(
    message_text: str,
    *,
    governing_topic: Optional[str] = None,
    knowledge_context: str = "",
    dynamic_requirements: Sequence[Any] = (),
    product_lexicon: Any = (),
    product_lexicon_available: bool = True,
) -> Dict[str, str]:
    """What this turn needs, and why.

    Returns {"intent": ..., "data_required": ..., "reason": ...} where
    data_required is "knowledge_only", "catalog" or "live".

    Checked most-binding first. Every branch above the Knowledge one is a
    BLOCKER: a signal that no approved document can answer this turn, however
    confidently Knowledge matched a topic. Only a turn that trips none of them
    may be called knowledge_only.
    """
    normalised = _normalise(message_text)

    def verdict(intent, data_required, reason):
        return {"intent": intent, "data_required": data_required, "reason": reason}

    # 1. Knowledge itself demanded a live check. Its own approved policy says
    #    this answer is invalid without fresh data.
    if dynamic_requirements:
        return verdict(
            INTENT_KNOWLEDGE_REQUIRES_LIVE, DATA_LIVE, "knowledge_requires_live_check"
        )

    # 2. A complaint about this customer's own order/product. Checked before
    #    everything else commercial because it is the costliest to get wrong,
    #    and before the generic returns policy so "¿cómo funcionan las
    #    devoluciones?" stays Knowledge while "me llegó roto" does not.
    if _INDIVIDUAL_CLAIM_RE.search(normalised) and _FIRST_PERSON_RE.search(normalised):
        return verdict(INTENT_INDIVIDUAL_CLAIM, DATA_LIVE, "individual_claim")

    # 3. An order that already exists -- including asking to collect ONE.
    #    Collecting is only an order question when it points at a specific
    #    order; "¿cómo puedo retirar un pedido?" asks how pickups work, which
    #    approved policy answers without touching the store.
    if (
        _EXISTING_ORDER_RE.search(normalised)
        or _ORDER_NUMBER_REFERENCE_RE.search(normalised)
        or _pickup_of_a_specific_order(normalised)
    ):
        return verdict(INTENT_EXISTING_ORDER, DATA_LIVE, "existing_order")

    # 4. Approved policy answers this, and nothing about the turn points at
    #    merchandise or at one order. Checked BEFORE the commercial branches
    #    precisely because those branches trigger on words like "quiero" and
    #    "tienen", which carry no commercial meaning by themselves. Kept
    #    narrow on purpose: a governing topic alone never wins -- it wins only
    #    when the message names no product, no price, no stock, no quantity
    #    and no specific order.
    #
    #    Requires the lexicon to be loaded: _carries_commercial_object asks it
    #    whether a product was named, and a blind check returning "no product"
    #    is not evidence of anything. Without it this branch would hand a
    #    Knowledge answer to a turn nobody had checked.
    if (
        governing_topic
        and (knowledge_context or "").strip()
        and product_lexicon_available
        and (
            not _carries_commercial_object(normalised, product_lexicon)
            # A wholesale enquiry names products and prices and is still
            # answered entirely by approved content.
            or _wholesale_information_request(normalised)
        )
    ):
        return verdict(
            INTENT_POLICY_QUESTION, DATA_KNOWLEDGE_ONLY, "governing_topic_answers_turn"
        )

    # 5-6. Commercial facts only the store holds.
    if _PRICE_REQUEST_RE.search(normalised):
        return verdict(INTENT_PRICE_REQUEST, DATA_LIVE, "price_requested")
    if _STOCK_REQUEST_RE.search(normalised):
        return verdict(INTENT_STOCK_REQUEST, DATA_LIVE, "stock_requested")

    # 6. Asking WHICH ONE to get. Fred does not advise, so this needs no data
    #    at all -- it needs Isa. Checked before the purchase and product
    #    branches so a request for a recommendation never becomes a catalog
    #    search whose only possible output is a recommendation.
    named = _named_catalog_product(normalised, product_lexicon)
    if _ADVICE_REQUEST_RE.search(normalised):
        return verdict(INTENT_ADVICE_REQUEST, DATA_KNOWLEDGE_ONLY, "advice_request")
    #    Describing a need without naming anything concrete is the same
    #    request in other words ("estoy buscando unas pestañas naturales").
    if not named and _VAGUE_PRODUCT_SEARCH_RE.search(normalised):
        return verdict(INTENT_ADVICE_REQUEST, DATA_KNOWLEDGE_ONLY, "vague_product_search")

    # 7. Deciding to buy. Fred does not close sales: this is a handoff carrying
    #    whatever the customer already said, never a checkout.
    if _PURCHASE_INTENT_RE.search(normalised):
        return verdict(INTENT_PURCHASE_INTENT, DATA_LIVE, "purchase_intent")

    # 8. A real product name, taken from the real catalog -- or a product
    #    category, which is still the customer pointing at merchandise.
    #    Objective questions about it stay Fred's.
    if named:
        return verdict(INTENT_PRODUCT_NAMED, DATA_CATALOG, "product_named")
    if _PRODUCT_CATEGORY_RE.search(normalised):
        return verdict(INTENT_PRODUCT_NAMED, DATA_CATALOG, "product_category")

    # 8. Choosing within a product (colour, size, variant, quantity unit).
    if _VARIANT_ATTRIBUTE_RE.search(normalised):
        return verdict(INTENT_VARIANT_ATTRIBUTE, DATA_CATALOG, "variant_attribute")

    # 9. Pointing at something from earlier in the conversation.
    if _ANAPHORIC_REFERENCE_RE.search(normalised):
        return verdict(
            INTENT_ANAPHORIC_REFERENCE, DATA_CATALOG, "anaphoric_reference"
        )

    # 10. Wanting a product without naming one.
    if _PRODUCT_INTEREST_RE.search(normalised):
        return verdict(INTENT_PRODUCT_INTEREST, DATA_CATALOG, "product_interest")

    # 11. Nothing commercial, nothing personal, and Knowledge retrieved a
    #     confident governing answer: policies, hours, showroom, generic
    #     returns, generic payment methods, generic shipping.
    #
    #     Guarded by the lexicon being genuinely loaded. Without it, check 7
    #     above was blind -- it could not have detected a named product, so
    #     "no blocker fired" proves nothing and knowledge_only would be an
    #     unearned conclusion. Fail closed: keep spending, and say why.
    if not product_lexicon_available:
        return verdict(INTENT_UNKNOWN, DATA_CATALOG, "product_lexicon_unavailable")

    # 12. Anything left. A governing topic on its own does NOT win here: the
    #     only branch that may conclude knowledge_only is the guarded one
    #     above, which first checks that the turn names nothing commercial.
    #     Reaching this point with a topic means the message carried something
    #     commercial that no earlier branch recognised -- "pedido 6345" names
    #     an order without matching any order phrase -- so it keeps its
    #     lookups, and the reason says which of the two situations it was.
    return verdict(
        INTENT_UNKNOWN, DATA_CATALOG,
        "commercial_signal_unmatched" if governing_topic else "no_governing_topic",
    )


def _order_status_needs_isa(dynamic_requirements: Sequence[Any]) -> Optional[str]:
    """A verified order_status result can itself demand Isa, deterministically
    -- not left to the model's judgment: the order genuinely doesn't exist,
    or the data is internally inconsistent (marked shipped/delivered with no
    tracking on file). Returns None when neither condition is met."""
    for item in dynamic_requirements:
        if getattr(item, "fact", "") != "order_status" or getattr(item, "status", "") != "completed":
            continue
        result = getattr(item, "result", {}) or {}
        if result.get("found") is False:
            return "order_not_found"
        # A pickup has no tracking number by design, and most delivered
        # orders in this store are pickups: measured on 40 real orders, 14 of
        # 21 DELIVERED had no tracking and every one of those was a pickup.
        # Treating that as an inconsistency would have sent a large share of
        # perfectly normal collected orders to Isa as broken data.
        if str(result.get("shipping_type") or "").lower() == "pickup":
            continue
        status_text = _normalise("{} {} {}".format(
            result.get("status") or "",
            result.get("shipping_status") or "",
            result.get("fulfillment_status") or "",
        ))
        shipped_or_delivered = any(
            keyword in status_text
            for keyword in ("enviado", "entregado", "despachado", "shipped",
                            "delivered", "dispatched")
        )
        if shipped_or_delivered and not result.get("tracking"):
            return "order_status_contradiction"
    return None


def resolve_harness_routing(
    message_text: str,
    prior_history: Sequence[Mapping[str, Any]],
    *,
    decision: Optional[Mapping[str, Any]] = None,
    handoff: Optional[Mapping[str, Any]] = None,
    knowledge_retrieval: Any = None,
    dynamic_requirements: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Resolve one routing result from the same evidence in app and shadow."""
    effective_decision = dict(decision or {})
    effective_handoff = dict(handoff or {}) if handoff else None
    governing_topic = getattr(knowledge_retrieval, "governing_topic", None)
    obligations = getattr(knowledge_retrieval, "obligations", None)

    unavailable_requiring_isa = next((
        item for item in dynamic_requirements
        if getattr(item, "status", "") in {"unavailable_tool", "failed"}
        and "isa" in _normalise(getattr(item, "customer_fallback", ""))
    ), None)
    # A missing live verifier is a limit on ONE datum, not a reason to throw
    # away an approved answer. When a governing topic was retrieved (and that
    # topic does not itself demand review -- that case is the branch below),
    # the approved policy still answers the question and the unavailable check
    # stays what it actually is: guidance on wording. Promoting it to a
    # handoff is what made Fred reply "El dato live requerido no tiene
    # verificador disponible" to a showroom question the KB already covers.
    if unavailable_requiring_isa and governing_topic and not (
        obligations and obligations.escalation_required
    ):
        unavailable_requiring_isa = None
    order_status_isa_reason = _order_status_needs_isa(dynamic_requirements)

    if governing_topic and obligations and obligations.escalation_required:
        reason = (
            "special_sale_request"
            if legacy_special_sale_context(message_text, prior_history)
            else "unable_to_verify"
        )
        effective_handoff = {
            "reason": reason,
            "summary": "El topic aprobado requiere revisión de Isa para este caso.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": reason,
            "summary": "Routing determinado por obligaciones del topic primario.",
        }
        source = "primary_topic_obligation"
    elif unavailable_requiring_isa:
        # If the approved fallback explicitly requires Isa, that next step is
        # routing state, not merely optional wording in the final response.
        effective_handoff = {
            "reason": "unable_to_verify",
            "summary": "El dato live requerido no tiene verificador disponible.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": "unable_to_verify",
            "summary": "Routing determinado por un requerimiento dinámico no verificable.",
        }
        source = "dynamic_requirement_fallback"
    elif order_status_isa_reason:
        # A real, verified Tiendanube lookup already answered these -- no
        # model judgment needed or wanted: the order doesn't exist, or its
        # own data is inconsistent (marked shipped/delivered with no
        # tracking on file). Never guess or reassure the customer here.
        summaries = {
            "order_not_found": "La orden consultada no existe en Tiendanube.",
            "order_status_contradiction": (
                "El estado de la orden es inconsistente (marcado enviado/entregado "
                "sin tracking registrado)."
            ),
        }
        effective_handoff = {
            "reason": "unable_to_verify",
            "summary": summaries[order_status_isa_reason],
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": "unable_to_verify",
            "summary": summaries[order_status_isa_reason],
        }
        source = "order_status_" + order_status_isa_reason
    elif not governing_topic and legacy_special_sale_context(message_text, prior_history):
        # Knowledge can be disabled or fail. In that degraded mode, retain the
        # established safety boundary instead of allowing a normal checkout.
        effective_handoff = {
            "reason": "special_sale_request",
            "summary": "Caso especial sin topic aprobado confiable.",
        }
        effective_decision = {
            "action": "handoff_to_isa",
            "reason": "special_sale_request",
            "summary": "Fallback conservador de venta especial.",
        }
        source = "legacy_safety_fallback"
    else:
        source = "agent_decision"

    return {
        "decision": effective_decision,
        "handoff": effective_handoff,
        "source": source,
        "governing_topic": governing_topic,
    }


def visible_routing_contract(
    routing: Mapping[str, Any],
    *,
    dynamic_requirements: Sequence[Any] = (),
) -> Dict[str, Any]:
    """Describe what the customer-facing reply must communicate.

    This is deliberately domain-neutral. It translates the effective routing
    state into a small language contract instead of maintaining a second set
    of business decisions in the response layer.
    """
    decision = dict(routing.get("decision") or {})
    handoff = dict(routing.get("handoff") or {}) if routing.get("handoff") else None
    action = "handoff_to_isa" if handoff else str(decision.get("action") or "reply")
    reason = str((handoff or {}).get("reason") or decision.get("reason") or "normal_response")
    missing = []
    unavailable = []
    unavailable_messages = []
    for requirement in dynamic_requirements:
        status = getattr(requirement, "status", "")
        if status == "missing_arguments":
            missing.extend(getattr(requirement, "missing_arguments", ()) or ())
        elif status in {"unavailable_tool", "failed"}:
            unavailable.append(getattr(requirement, "fact", ""))
            message = str(getattr(requirement, "customer_fallback", "") or "")
            if message:
                unavailable_messages.append(message)
    return {
        "action": action,
        "reason": reason,
        # Presentation metadata only. These fields do not alter routing; they
        # let the final style layer preserve a genuinely necessary discovery
        # question when no product could be identified.
        "response_mode": str(decision.get("response_mode") or ""),
        "match_type": str(decision.get("match_type") or ""),
        # Which live checks actually returned a verified Tiendanube fact this
        # turn (set by agent.py only after a real get_stock/get_product_
        # availability call succeeded). This is the evidence that a discovery
        # answer is grounded, not merely a syntactically valid decision.
        "checks_completed": list(decision.get("checks_completed") or ()),
        # What this specific decision itself said it needed verified — the
        # other half of the grounded-discovery evidence. "Something got
        # checked" is not the same claim as "what this decision needed got
        # checked"; both fields are required to tell them apart.
        "required_checks": list(decision.get("required_checks") or ()),
        "next_step": (
            "isa_review" if action == "handoff_to_isa"
            else "provide_missing_information" if missing
            else "clarify" if action.startswith("clarify")
            else "continue"
        ),
        "missing_information": list(dict.fromkeys(item for item in missing if item)),
        "unavailable_facts": list(dict.fromkeys(item for item in unavailable if item)),
        "unavailable_messages": list(dict.fromkeys(
            item for item in unavailable_messages if item
        )),
    }


def align_reply_with_routing(
    reply: str,
    routing: Mapping[str, Any],
    *,
    dynamic_requirements: Sequence[Any] = (),
) -> str:
    """Make visible language compatible with the already-resolved action.

    The original answer remains available for useful domain guidance. This
    function only adds or replaces the routing/next-step layer; it never makes
    a new business decision.
    """
    contract = visible_routing_contract(
        routing, dynamic_requirements=dynamic_requirements
    )
    labels = {
        "order_number": "el número de orden",
        "sku": "el modelo exacto o su link",
    }
    missing = [labels.get(item, item.replace("_", " ")) for item in contract["missing_information"]]
    base = str(reply or "").strip()

    if contract["action"] == "handoff_to_isa":
        if missing:
            return (
                "Para verificar bien este caso antes de que lo revise Isa, me falta {}. "
                "Con ese dato se lo paso junto con todo el contexto."
            ).format(" y ".join(missing))
        if contract["unavailable_messages"]:
            # Deliberately replaces the base here: by this point the turn is
            # genuinely escalating, and leaving a dangling clarifying question
            # ("¿qué producto buscás?") would invite an answer nobody will act
            # on. An approved Knowledge answer never reaches this branch any
            # more -- the guard in resolve_harness_routing keeps a governing
            # topic from being escalated over one unverifiable datum.
            base = " ".join(contract["unavailable_messages"])
        clauses = []
        if "isa" not in _normalise(base):
            reasons = {
                "special_sale_request": "Este caso necesita una validación comercial de Isa.",
                "unable_to_verify": "No pude confirmar de forma segura todo lo necesario, así que lo revisa Isa.",
                "safety_concern": "Por seguridad, este caso necesita que lo revise Isa.",
            }
            clauses.append(reasons.get(contract["reason"], "Este caso necesita que lo revise Isa."))
        clauses.append("Se lo paso con lo que ya me contaste y seguimos por acá.")
        return "\n\n".join(part for part in (base, " ".join(clauses)) if part).strip()

    if missing:
        # A product-discovery turn that already resolved a real match against
        # verified Tiendanube facts must not be thrown away just because the
        # generic live-check gate also asked for a bare "sku" argument — the
        # product was already found and verified, there is nothing left to
        # ask the customer. This is deliberately narrow: it only waives the
        # "sku" argument (never order_number or anything else), only when the
        # agent's own decision says product_discovery, only when it landed on
        # an actual match (not "no_match"), and only when a live check truly
        # completed this turn (checks_completed is never set from free-form
        # model text — see agent.py). A lookup or a genuine ambiguity has no
        # completed check to point to, so this never bypasses those.
        #
        # required_checks <= checks_completed, not merely bool(checks_completed):
        # "something was checked" is not the same claim as "what this decision
        # said it needed got checked". A decision that only proved live_stock
        # (e.g. via search_available_products) while still owing live_price
        # (the customer asked for a price) must not be treated as grounded —
        # required_checks is exactly the set that would let that distinction
        # through a bare truthiness check.
        grounded_discovery = (
            set(contract["missing_information"]) <= {"sku"}
            and contract["response_mode"] == "product_discovery"
            and contract["match_type"] not in ("", "no_match")
            and set(contract["required_checks"]) <= set(contract["checks_completed"])
        )
        if not grounded_discovery:
            # A missing live-check argument is the only useful next step.
            # Avoid a free-form answer that asks several unrelated questions.
            return "Para poder verificarlo en vivo, me falta {}.".format(" y ".join(missing))

    if contract["unavailable_messages"]:
        if contract["action"].startswith("clarify"):
            clarification = "Para seguir, necesito que me aclares el modelo exacto o me pases su link."
            return "\n\n".join(part for part in (
                base,
                " ".join(contract["unavailable_messages"]),
                clarification,
            ) if part).strip()
        return "\n\n".join(
            part for part in (base, " ".join(contract["unavailable_messages"])) if part
        ).strip()

    if contract["action"].startswith("clarify"):
        clarification = "Para seguir, necesito que me aclares el modelo exacto o me pases su link."
        if _normalise(clarification) not in _normalise(base):
            return "\n\n".join(part for part in (base, clarification) if part).strip()

    return base
