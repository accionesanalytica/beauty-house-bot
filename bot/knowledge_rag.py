"""Versioned, reviewable knowledge retrieval for Fred.

Knowledge is deliberately separated from live commerce. Curated documents may
explain stable facts and procedures, but Tiendanube/tools remain authoritative
for price, stock, checkout, tracking and current order/payment state.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


KNOWLEDGE_CHUNK_CHARS = 900
KNOWLEDGE_CHUNK_OVERLAP = 120
DEFAULT_MIN_KNOWLEDGE_SIMILARITY = 0.68
DEFAULT_KNOWLEDGE_TOP_K = 6
ALLOWED_KNOWLEDGE_TYPES = {"fact", "procedure", "faq"}
ALLOWED_RISK_LEVELS = {"low", "medium", "high"}
REQUIRED_METADATA_FIELDS = {
    "id", "topic", "knowledge_type", "source", "approved_by",
    "reviewed_at", "risk_level", "requires_isa_confirmation",
}
URL_PATTERN = re.compile(r"https://[^\s)>]+")


@dataclass(frozen=True)
class KnowledgeChunk:
    source_id: str
    section: str
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeObligations:
    topics: tuple[str, ...] = ()
    required_disclosures: tuple[Mapping[str, Any], ...] = ()
    required_links: tuple[Mapping[str, Any], ...] = ()
    optional_links: tuple[Mapping[str, Any], ...] = ()
    escalation_required: bool = False


@dataclass(frozen=True)
class DynamicRequirement:
    fact: str
    verifier: str
    status: str
    required_arguments: tuple[str, ...] = ()
    missing_arguments: tuple[str, ...] = ()
    arguments: Mapping[str, Any] = field(default_factory=dict)
    fallback: str = ""
    customer_fallback: str = ""


@dataclass(frozen=True)
class KnowledgeRetrieval:
    rows: tuple[Mapping[str, Any], ...] = ()
    context: str = ""
    obligations: KnowledgeObligations = field(default_factory=KnowledgeObligations)
    retrieved_topics: tuple[str, ...] = ()
    governing_topic: Optional[str] = None
    topic_scores: tuple[Mapping[str, Any], ...] = ()
    governing_reason: str = ""
    discarded_obligations: tuple[Mapping[str, Any], ...] = ()
    dynamic_requirements: tuple[DynamicRequirement, ...] = ()


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^a-zA-Z0-9@./:_-]+", " ", value.lower()).split())


def _tokens(value: str) -> set[str]:
    stop = {
        "a", "al", "algo", "como", "con", "de", "del", "el", "en", "es",
        "esta", "hay", "la", "las", "lo", "los", "me", "mi", "para", "por",
        "que", "se", "si", "su", "te", "tengo", "un", "una", "y", "yo",
    }
    aliases = {
        "mayor": "mayorista", "mayoreo": "mayorista", "mayoristas": "mayorista",
        "reintegren": "reembolso", "reintegro": "reembolso", "reintegrar": "reembolso",
        "devolver": "devolucion", "devuelvan": "devolucion", "devoluciones": "devolucion",
        "distinto": "equivocado", "distinta": "equivocado", "incorrecto": "equivocado",
        "incorrecta": "equivocado", "envio": "pedido", "paquete": "pedido",
        "arden": "reaccion", "ardiendo": "reaccion", "ardor": "reaccion",
        "reacciones": "reaccion", "irritacion": "reaccion", "ojos": "ocular",
        "publicada": "publicado", "publicadas": "publicado", "publicados": "publicado",
    }
    return {
        aliases.get(token, token)
        for token in _normalise(value).split()
        if len(token) > 2 and token not in stop
    }


# Real evidence of a previous purchase the customer wants tracked -- not
# just words that happen to score against the order_tracking knowledge
# topic (that scoring is fuzzy; this gate is the hard customer-facing
# boundary for actually demanding an order number). Operates on already
# accent-stripped, lowercased text (see _normalise).
_TRACKING_EVIDENCE_RE = re.compile(
    r"\bmi\s+pedido\b|\bmi\s+orden\b|\btracking\b|\bseguimiento\b|"
    r"numero\s+de\s+(?:orden|pedido)|"
    r"donde\s+esta\s+mi\s+(?:compra|pedido|orden)|"
    r"no\s+me\s+lleg[oa]\b"
)

# A customer rarely puts the order number immediately after "orden"/"pedido":
# "el número es 1234", "mi pedido es el 1234", "pedido: 1234" are all real
# phrasings that a same-message-only, adjacent-word regex misses. Allow a
# short run of filler words between the keyword and the digits, with a word
# boundary so "pedidos" (plural) can't match and swallow an unrelated number
# later in the sentence. Shared with app.py's deterministic tracking-flow
# router, not just this module, so both extract an order number identically.
ORDER_NUMBER_RE = re.compile(
    r"\b(?:orden|pedido|numero)\b\D{0,20}?(\d{4,})|#(\d{4,})",
)


def extract_order_number(query: str) -> Optional[str]:
    """The order number a customer's own message names, if any -- accent/case
    insensitive, same extraction used by infer_dynamic_requirements below."""
    match = ORDER_NUMBER_RE.search(_normalise(query))
    if not match:
        return None
    return match.group(1) or match.group(2)


def canonical_knowledge_embedding_text(chunk: KnowledgeChunk) -> str:
    """Return the single semantic representation used by every retriever.

    Operational metadata (IDs, approval/review fields, flags and mandatory
    URLs) is intentionally excluded: it is needed for governance after
    retrieval, but it must not influence semantic ranking.
    """
    metadata = chunk.metadata or {}

    def semantic(value: Any) -> str:
        without_urls = URL_PATTERN.sub(" ", str(value or ""))
        return " ".join(without_urls.split())

    topic = semantic(metadata.get("topic")).replace("_", " ")
    section = semantic(chunk.section)
    keywords = [
        cleaned
        for cleaned in (semantic(keyword) for keyword in metadata.get("keywords") or [])
        if cleaned
    ]
    content = semantic(chunk.content)
    parts = []
    if topic:
        parts.append("Topic: {}".format(topic))
    if section:
        parts.append("Sección: {}".format(section))
    if keywords:
        parts.append("Palabras clave: {}".format(", ".join(keywords)))
    if content:
        parts.append("Contenido: {}".format(content))
    return "\n".join(parts)


def recent_conversation_retrieval_query(
    message: str,
    history: Sequence[Mapping[str, Any]],
    *,
    max_customer_turns: int = 2,
) -> str:
    """Add recent customer context only for a retrieval fallback.

    Callers must first try the current message by itself.  This helper exists
    for short follow-ups such as "lo coordiné por Instagram" whose meaning
    depends on the immediately preceding customer turn.  Assistant replies are
    deliberately excluded so Fred cannot retrieve from facts he generated.
    """
    previous = [
        str(item.get("content") or "").strip()
        for item in history or ()
        if item.get("role") in {"user", "customer"}
        and str(item.get("content") or "").strip()
    ][-max_customer_turns:]
    parts = previous + [str(message or "").strip()]
    return "\n".join(part for part in parts if part)


def retrieve_with_recent_context(
    message: str,
    history: Sequence[Mapping[str, Any]],
    retriever: Any,
) -> tuple[KnowledgeRetrieval, str, bool]:
    """Try the current turn first, then one bounded conversational fallback."""
    retrieval = retriever(message)
    if retrieval.governing_topic or not history:
        return retrieval, message, False
    fallback_query = recent_conversation_retrieval_query(message, history)
    if fallback_query == message:
        return retrieval, message, False
    fallback = retriever(fallback_query)
    return fallback, fallback_query, True


def _split_frontmatter(markdown: str) -> tuple[Dict[str, Any], str]:
    """Parse strict JSON frontmatter delimited by ``---``.

    JSON is intentional: it is standard-library only and fails closed instead
    of accepting ambiguous YAML values in approved business knowledge.
    """
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise ValueError("Frontmatter sin cierre ---") from error
    raw = "\n".join(lines[1:end]).strip()
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("Frontmatter debe ser JSON válido: {}".format(error)) from error
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter debe ser un objeto JSON")
    return metadata, "\n".join(lines[end + 1:])


def validate_metadata(metadata: Mapping[str, Any], *, source: str = "documento") -> Dict[str, Any]:
    if metadata.get("index") is False:
        return dict(metadata)
    missing = sorted(field for field in REQUIRED_METADATA_FIELDS if field not in metadata)
    if missing:
        raise ValueError("{}: metadata faltante: {}".format(source, ", ".join(missing)))
    if metadata["knowledge_type"] not in ALLOWED_KNOWLEDGE_TYPES:
        raise ValueError("{}: knowledge_type inválido".format(source))
    if metadata["risk_level"] not in ALLOWED_RISK_LEVELS:
        raise ValueError("{}: risk_level inválido".format(source))
    if not isinstance(metadata["requires_isa_confirmation"], bool):
        raise ValueError("{}: requires_isa_confirmation debe ser booleano".format(source))
    for key in ("required_disclosures", "required_links", "optional_links", "keywords"):
        if key in metadata and not isinstance(metadata[key], list):
            raise ValueError("{}: {} debe ser una lista".format(source, key))
    for key in ("required_links", "optional_links"):
        for link in metadata.get(key, []):
            if link.get("link_type") != "approved_static_link":
                raise ValueError("{}: sólo se indexan approved_static_link".format(source))
            url = str(link.get("url") or "")
            if not re.fullmatch(r"https://[^\s]+", url):
                raise ValueError("{}: URL aprobada inválida".format(source))
    return dict(metadata)


def _split_long_text(text: str) -> List[str]:
    text = " ".join(text.split())
    if len(text) <= KNOWLEDGE_CHUNK_CHARS:
        return [text] if text else []
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + KNOWLEDGE_CHUNK_CHARS, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - KNOWLEDGE_CHUNK_OVERLAP, start + 1)
    return chunks


def chunk_markdown(
    source_id: str,
    markdown: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> List[KnowledgeChunk]:
    """Chunk Markdown by semantic heading boundaries, then bounded windows."""
    parsed_metadata, body = _split_frontmatter(markdown)
    document_metadata = validate_metadata(
        metadata or parsed_metadata, source=source_id
    ) if (metadata or parsed_metadata) else {}
    if document_metadata.get("index") is False:
        return []

    sections: List[tuple[str, List[str]]] = []
    heading = "Información general"
    lines: List[str] = []

    def flush() -> None:
        if lines:
            sections.append((heading, list(lines)))
            lines.clear()

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip() or "Información general"
        elif stripped:
            lines.append(stripped)
    flush()

    chunks: List[KnowledgeChunk] = []
    for section, section_lines in sections:
        for content in _split_long_text(" ".join(section_lines)):
            chunks.append(KnowledgeChunk(source_id, section, content, document_metadata))
    return chunks


def load_knowledge_chunks(directory: str | Path) -> List[KnowledgeChunk]:
    """Recursively load only curated, metadata-valid Markdown knowledge."""
    root = Path(directory)
    chunks: List[KnowledgeChunk] = []
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        markdown = path.read_text(encoding="utf-8")
        metadata, _ = _split_frontmatter(markdown)
        if metadata.get("index") is False:
            continue
        source_id = str(metadata.get("id") or path.relative_to(root).with_suffix(""))
        chunks.extend(chunk_markdown(source_id, markdown))
    return chunks


def approved_knowledge_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    limit: int,
    min_similarity: float = DEFAULT_MIN_KNOWLEDGE_SIMILARITY,
) -> List[Dict[str, Any]]:
    accepted = [
        row for row in rows
        if row.get("status") == "approved"
        and bool(row.get("active", True))
        and float(row.get("similarity") or 0) >= min_similarity
    ]
    return sorted(accepted, key=lambda row: -float(row["similarity"]))[:limit]


def _matches_trigger(trigger: str, query: str) -> bool:
    normalised_trigger = _normalise(trigger)
    normalised_query = _normalise(query)
    if normalised_trigger in normalised_query:
        return True
    # For a phrase, matching a single surviving token (for example "días"
    # from "5 días") is too broad. Token aliases are used only for a single
    # semantic term such as ardor/reacción.
    raw_words = normalised_trigger.split()
    trigger_tokens = _tokens(trigger)
    if (
        raw_words[:1] == ["no"]
        and "no" in normalised_query.split()
        and trigger_tokens
        and trigger_tokens.issubset(_tokens(query))
    ):
        return True
    return bool(len(raw_words) == 1 and trigger_tokens and trigger_tokens.issubset(_tokens(query)))


def _triggered(item: Mapping[str, Any], query: str) -> bool:
    triggers = item.get("when_any") or []
    if not triggers:
        return True
    return any(_matches_trigger(str(trigger), query) for trigger in triggers)


def collect_topic_obligations(
    rows: Sequence[Mapping[str, Any]], query: str = ""
) -> KnowledgeObligations:
    topics: List[str] = []
    disclosures: Dict[str, Mapping[str, Any]] = {}
    required_links: Dict[str, Mapping[str, Any]] = {}
    optional_links: Dict[str, Mapping[str, Any]] = {}
    escalation_required = False
    for row in rows:
        metadata = row.get("metadata") or {}
        topic = str(metadata.get("topic") or "").strip()
        if topic and topic not in topics:
            topics.append(topic)
        escalation_triggers = metadata.get("escalation_when_any", [])
        trigger_matched = any(_matches_trigger(str(trigger), query) for trigger in escalation_triggers)
        informational_query = bool(re.match(
            r"^(?:como|que|cual|cuales|puedo|se puede)\b", _normalise(query)
        )) and not bool(re.search(
            r"\b(?:quiero|necesito|me mandaron|me llego|devolver|reintegrar|cambiar)\b",
            _normalise(query),
        ))
        # A document can require Isa only for named situations. In that case
        # the trigger list narrows the rule; it must not escalate every query
        # that happens to retrieve the same broad procedure.
        if (trigger_matched and not informational_query) or (
            bool(metadata.get("requires_isa_confirmation"))
            and not escalation_triggers
        ):
            escalation_required = True
        for key, target in (
            ("required_disclosures", disclosures),
            ("required_links", required_links),
            ("optional_links", optional_links),
        ):
            for item in metadata.get(key, []):
                if _triggered(item, query):
                    identifier = str(item.get("id") or item.get("url") or item.get("text"))
                    target[identifier] = item
    return KnowledgeObligations(
        topics=tuple(topics),
        required_disclosures=tuple(disclosures.values()),
        required_links=tuple(required_links.values()),
        optional_links=tuple(optional_links.values()),
        escalation_required=escalation_required,
    )


def format_knowledge_context(
    rows: Sequence[Mapping[str, Any]],
    obligations: Optional[KnowledgeObligations] = None,
    *,
    governing_topic: Optional[str] = None,
    dynamic_requirements: Sequence[DynamicRequirement] = (),
) -> str:
    if not rows:
        return ""
    obligations = obligations or collect_topic_obligations(rows)
    lines = ["Conocimiento aprobado recuperado (no reemplaza datos vigentes):"]
    if governing_topic:
        lines.append("Topic gobernante: {}. Sólo sus obligaciones son vinculantes.".format(governing_topic))
    else:
        lines.append(
            "No hay topic gobernante con confianza suficiente. Usá los resultados sólo como contexto; "
            "no combines procedimientos ni afirmes hechos dudosos."
        )
    for row in rows:
        source = row.get("source_id") or "fuente interna"
        section = row.get("section") or "sin sección"
        content = " ".join(str(row.get("content") or "").split())
        lines.append("- [{} / {}] {}".format(source, section, content))
    if obligations.required_disclosures or obligations.required_links:
        lines.append("Obligaciones del topic para esta respuesta:")
        for item in obligations.required_disclosures:
            lines.append("- DEBE incluir: {}".format(item.get("text")))
        for item in obligations.required_links:
            lines.append("- DEBE enviar esta URL aprobada exactamente: {}".format(item.get("url")))
    if obligations.escalation_required:
        lines.append("- ROUTING: este caso requiere derivación a Isa después de reunir la información inicial.")
    if dynamic_requirements:
        lines.append("Requisitos dinámicos antes de responder:")
        for requirement in dynamic_requirements:
            if requirement.status == "missing_arguments":
                lines.append(
                    "- {} requiere {}. Falta: {}. {}".format(
                        requirement.fact,
                        requirement.verifier,
                        ", ".join(requirement.missing_arguments),
                        requirement.fallback,
                    )
                )
            elif requirement.status == "unavailable_tool":
                lines.append(
                    "- {} no tiene herramienta live disponible. {}".format(
                        requirement.fact, requirement.fallback
                    )
                )
            else:
                lines.append(
                    "- {} debe verificarse con {} antes de afirmarlo.".format(
                        requirement.fact, requirement.verifier
                    )
                )
    lines.append(
        "Precio, stock, pago, checkout, tracking, estado de orden y horarios disponibles "
        "deben verificarse con Tiendanube o Isa mediante una herramienta live. Nunca inventes una URL."
    )
    return "\n".join(lines)


def _keyword_topic_signal(metadata: Mapping[str, Any], query: str) -> tuple[float, List[str]]:
    normalised_query = _normalise(query)
    query_tokens = _tokens(query)
    matched: List[str] = []
    score = 0.0
    terms = list(metadata.get("keywords") or []) + list(metadata.get("escalation_when_any") or [])
    for raw_term in terms:
        term = str(raw_term).strip()
        normalised_term = _normalise(term)
        term_tokens = _tokens(term)
        if not normalised_term or not term_tokens:
            continue
        if normalised_term in normalised_query:
            value = 0.12 if len(term_tokens) > 1 else 0.08
        elif term_tokens.issubset(query_tokens):
            value = 0.07
        else:
            continue
        if term not in matched:
            matched.append(term)
            score += value
    return min(score, 0.24), matched


def choose_primary_topic(
    rows: Sequence[Mapping[str, Any]], query: str,
    *, metadata_rows: Sequence[Mapping[str, Any]] = (),
) -> tuple[Optional[str], tuple[Mapping[str, Any], ...], str]:
    """Choose one governing topic while retaining all retrieved topics as context.

    Generic FAQ chunks may rank highly because they repeat customer wording.
    They remain useful context, but cannot govern when a more specific approved
    domain has direct keyword evidence. This uses knowledge type/metadata rather
    than special-casing business topics.
    """
    by_topic: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        metadata = row.get("metadata") or {}
        topic = str(metadata.get("topic") or "").strip()
        if not topic:
            continue
        entry = by_topic.setdefault(topic, {
            "topic": topic,
            "max_similarity": 0.0,
            "similarities": [],
            "keyword_boost": 0.0,
            "matched_keywords": [],
            "specific": False,
        })
        entry["max_similarity"] = max(
            float(entry["max_similarity"]), float(row.get("similarity") or 0)
        )
        entry["similarities"].append(float(row.get("similarity") or 0))
        boost, matched = _keyword_topic_signal(metadata, query)
        entry["keyword_boost"] = max(float(entry["keyword_boost"]), boost)
        entry["matched_keywords"] = list(dict.fromkeys(entry["matched_keywords"] + matched))
        if metadata.get("knowledge_type") != "faq":
            entry["specific"] = True

    # Top-K stays small for prompt quality, while approved metadata from those
    # retrieved topics can still provide stronger intent evidence.
    for row in metadata_rows:
        metadata = row.get("metadata") or {}
        topic = str(metadata.get("topic") or "").strip()
        if topic not in by_topic:
            continue
        boost, matched = _keyword_topic_signal(metadata, query)
        entry = by_topic[topic]
        entry["keyword_boost"] = max(float(entry["keyword_boost"]), boost)
        entry["matched_keywords"] = list(dict.fromkeys(entry["matched_keywords"] + matched))
        if metadata.get("knowledge_type") != "faq":
            entry["specific"] = True

    has_specific_signal = any(
        item["specific"] and (
            item["keyword_boost"] > 0 or item["max_similarity"] >= 0.16
        )
        for item in by_topic.values()
    )
    scored: List[Dict[str, Any]] = []
    for item in by_topic.values():
        generic_penalty = 0.12 if has_specific_signal and not item["specific"] else 0.0
        close_support = sum(
            1 for similarity in item.pop("similarities", [])
            if item["max_similarity"] - similarity <= 0.04
        )
        # Several independently retrieved chunks that closely support the same
        # topic are modest evidence against a near-tie.  The cap prevents a
        # topic with many chunks from winning by volume alone.
        support_bonus = min(max(close_support - 1, 0) * 0.005, 0.015)
        item["support_count"] = close_support
        item["support_bonus"] = support_bonus
        item["score"] = round(
            item["max_similarity"] + item["keyword_boost"] + support_bonus
            - generic_penalty, 6
        )
        item["eligible"] = not (has_specific_signal and not item["specific"])
        scored.append(item)
    scored.sort(key=lambda item: item["score"], reverse=True)
    eligible = [item for item in scored if item["eligible"]]
    if not eligible or eligible[0]["score"] < 0.18:
        return None, tuple(scored), "Sin candidato específico con confianza mínima (0.18)."
    winner = eligible[0]
    runner = eligible[1] if len(eligible) > 1 else None
    margin = winner["score"] - (runner["score"] if runner else 0.0)
    if runner and margin < 0.02 and winner["keyword_boost"] <= runner["keyword_boost"]:
        return None, tuple(scored), "Empate entre topics sin evidencia suficiente para gobernar."
    reason = (
        "{} elegido: score {:.3f}, similitud {:.3f}, boost {:.3f}, margen {:.3f}.".format(
            winner["topic"], winner["score"], winner["max_similarity"],
            winner["keyword_boost"], margin,
        )
    )
    return str(winner["topic"]), tuple(scored), reason


def infer_dynamic_requirements(
    query: str,
    governing_topic: Optional[str],
    *,
    available_arguments: Optional[Mapping[str, Any]] = None,
) -> tuple[DynamicRequirement, ...]:
    """Describe live facts without executing tools or inventing integrations."""
    if not governing_topic:
        return ()
    args = dict(available_arguments or {})
    normalised = _normalise(query)
    requirements: List[DynamicRequirement] = []
    order_match = ORDER_NUMBER_RE.search(normalised)
    if order_match:
        args.setdefault("order_number", order_match.group(1) or order_match.group(2))

    def add(
        fact: str, verifier: str, required: Sequence[str], fallback: str,
        *, tool_available: bool = True,
        customer_fallback: str = "",
    ) -> None:
        missing = tuple(name for name in required if not args.get(name))
        status = "unavailable_tool" if not tool_available else (
            "missing_arguments" if missing else "ready"
        )
        requirements.append(DynamicRequirement(
            fact=fact, verifier=verifier, status=status,
            required_arguments=tuple(required), missing_arguments=missing,
            arguments={name: args.get(name) for name in required if args.get(name)},
            fallback=fallback,
            customer_fallback=customer_fallback,
        ))

    if governing_topic == "order_tracking" and (order_match or _TRACKING_EVIDENCE_RE.search(normalised)):
        # governing_topic alone is not enough: keyword/embedding topic scoring
        # is fuzzy (e.g. "envío" is aliased to "pedido" for matching purposes,
        # see _tokens above) and can tip a plain shipping-cost or product
        # question into this topic. Demanding an order number is a hard
        # customer-facing boundary, so it requires the customer's own message
        # to actually say something tracking-shaped -- an order number, "mi
        # pedido/orden", "no me llegó", "tracking/seguimiento" -- not just a
        # topic-classifier side effect.
        add(
            "order_status", "get_order_status", ("order_number",),
            "Pedí el número de orden; si no existe o no se encuentra, derivá a Isa sin inventar estado.",
            customer_fallback=(
                "No puedo confirmar el estado sin verificar la orden; con el número de pedido "
                "puedo revisarlo y, si no aparece, lo vemos con Isa."
            ),
        )
    post_sale_tokens = {"devolucion", "reembolso", "equivocado"}
    if governing_topic == "commercial_operations" and (
        _tokens(query) & post_sale_tokens
    ):
        add(
            "order_status", "get_order_status", ("order_number",),
            "Pedí sólo el número de orden para ubicar la compra antes de derivar el caso.",
            customer_fallback=(
                "Para ubicar la compra y pasarle el caso completo a Isa necesito el número de orden."
            ),
        )
    price_requested = bool(re.search(r"\b(precio|cuanto cuesta|cuanto sale|valor)\b", normalised))
    stock_requested = bool(re.search(r"\b(stock|disponible|disponibilidad|tienen)\b", normalised))
    product_topics = {"lashes_guidance", "touchup_kit"}
    if governing_topic in product_topics and (price_requested or stock_requested):
        add(
            "live_price" if price_requested else "live_stock",
            "get_stock", ("sku",),
            "Primero identificá un SKU; si no puede verificarse, explicá el límite sin inventar.",
            customer_fallback=(
                "Necesito identificar el modelo exacto o su link antes de confirmar precio o stock."
            ),
        )
    if governing_topic == "pickups_showroom" and re.search(
        r"\b(hoy|horario|hora|turno|disponible|disponibilidad|cuando)\b", normalised
    ):
        add(
            "calendar_availability", "unavailable_tool", (),
            "Compartí sólo el calendario aprobado o derivá a Isa; no confirmes un horario.",
            tool_available=False,
            customer_fallback=(
                "No puedo confirmar horarios disponibles en vivo desde acá; podés revisar el "
                "calendario aprobado o lo vemos con Isa."
            ),
        )
    # The wholesale list used to be unavailable, so any mention of "mayorista"
    # raised an unverifiable requirement telling Fred not to state a minimum or
    # a price. Isa has since approved the SHOOW TOOLS list (see
    # knowledge/facts/wholesale-shoow-tools.md), so that requirement now
    # contradicts approved data -- and, being a dynamic requirement, it also
    # forced an informational question down the live path. Closing a wholesale
    # ORDER is still Isa's, which the scope handoff decides before retrieval.
    if governing_topic == "commercial_operations" and re.search(
        r"\b(comision|recargo|descuento|promocion)\b", normalised
    ):
        add(
            "current_fees_discounts", "unavailable_tool", (),
            "Aclarar que la condición vigente debe verificarse con Isa.",
            tool_available=False,
            customer_fallback=(
                "Los descuentos, recargos o comisiones vigentes necesitan confirmación de Isa."
            ),
        )
    if governing_topic == "commercial_operations" and re.search(
        r"\b(courier|kilo|kilos)\b", normalised
    ) and price_requested:
        add(
            "current_courier_quote", "unavailable_tool", (),
            "Explicá la modalidad y derivá la cotización actual; no inventes tarifa.",
            tool_available=False,
            customer_fallback=(
                "No puedo confirmar la cotización actual del courier automáticamente; la puede revisar Isa."
            ),
        )
    return tuple(requirements)


def build_knowledge_retrieval(
    rows: Sequence[Mapping[str, Any]], *, query: str = "",
    obligation_rows: Optional[Sequence[Mapping[str, Any]]] = None,
    available_arguments: Optional[Mapping[str, Any]] = None,
) -> KnowledgeRetrieval:
    retrieved_topics = tuple(dict.fromkeys(
        str((row.get("metadata") or {}).get("topic") or "")
        for row in rows if (row.get("metadata") or {}).get("topic")
    ))
    all_obligation_rows = list(obligation_rows or rows)
    governing_topic, topic_scores, reason = choose_primary_topic(
        rows, query, metadata_rows=all_obligation_rows
    )
    governing_rows = [
        row for row in all_obligation_rows
        if str((row.get("metadata") or {}).get("topic") or "") == governing_topic
    ]
    obligations = collect_topic_obligations(governing_rows, query) if governing_topic else KnowledgeObligations()
    discarded: List[Mapping[str, Any]] = []
    for topic in retrieved_topics:
        if topic == governing_topic:
            continue
        topic_rows = [
            row for row in all_obligation_rows
            if str((row.get("metadata") or {}).get("topic") or "") == topic
        ]
        secondary = collect_topic_obligations(topic_rows, query)
        if secondary.required_disclosures or secondary.required_links or secondary.escalation_required:
            discarded.append({
                "topic": topic,
                "required_disclosures": [item.get("id") for item in secondary.required_disclosures],
                "required_links": [item.get("id") for item in secondary.required_links],
                "escalation_required": secondary.escalation_required,
            })
    dynamic_requirements = infer_dynamic_requirements(
        query, governing_topic, available_arguments=available_arguments
    )
    return KnowledgeRetrieval(
        rows=tuple(rows),
        obligations=obligations,
        retrieved_topics=retrieved_topics,
        governing_topic=governing_topic,
        topic_scores=topic_scores,
        governing_reason=reason,
        discarded_obligations=tuple(discarded),
        dynamic_requirements=dynamic_requirements,
        context=format_knowledge_context(
            rows, obligations, governing_topic=governing_topic,
            dynamic_requirements=dynamic_requirements,
        ),
    )


def extract_https_urls(text: str) -> List[str]:
    return [url.rstrip(".,;!?") for url in URL_PATTERN.findall(str(text or ""))]


def _disclosure_present(response: str, item: Mapping[str, Any]) -> bool:
    normalised = _normalise(response)
    required_terms = item.get("required_terms") or []
    if required_terms:
        return all(_normalise(term) in normalised for term in required_terms)
    return _normalise(str(item.get("text") or "")) in normalised


def enforce_knowledge_obligations(
    response: str,
    obligations: KnowledgeObligations,
    *,
    verified_dynamic_links: Sequence[str] = (),
) -> str:
    """Append missing mandatory content and remove unverified URLs.

    Static URLs can only come from approved metadata. Dynamic URLs can only be
    passed by a live tool/caller. This is a deterministic guardrail after the
    model, so mandatory knowledge does not depend on lucky chunk wording.
    """
    approved_static = {
        str(item.get("url")) for item in obligations.required_links + obligations.optional_links
        if item.get("link_type") == "approved_static_link" and item.get("url")
    }
    allowed_urls = approved_static | {str(url) for url in verified_dynamic_links}
    cleaned = response
    for url in extract_https_urls(response):
        if url not in allowed_urls:
            cleaned = cleaned.replace(url, "").replace("  ", " ")

    additions: List[str] = []
    for item in obligations.required_disclosures:
        if not _disclosure_present(cleaned, item):
            additions.append(str(item.get("text") or "").strip())
    for item in obligations.required_links:
        url = str(item.get("url") or "").strip()
        if url and url not in cleaned:
            additions.append(url)
    if additions:
        cleaned = "{}\n\n{}".format(cleaned.rstrip(), "\n".join(additions))
    return cleaned.strip()


def retrieve_local_knowledge(
    query: str,
    chunks: Sequence[KnowledgeChunk],
    *,
    limit: int = DEFAULT_KNOWLEDGE_TOP_K,
    available_arguments: Optional[Mapping[str, Any]] = None,
) -> KnowledgeRetrieval:
    """Deterministic local retrieval for tests/benchmarks (no embeddings)."""
    query_tokens = _tokens(query)
    scored: List[tuple[float, KnowledgeChunk]] = []
    for chunk in chunks:
        # Both engines see the same canonical semantic fields. The offline
        # lexical reference still weights a hit in section/content more than
        # a metadata-only keyword hit; pgvector supplies its own cosine score.
        canonical_text = canonical_knowledge_embedding_text(chunk)
        if not (query_tokens & _tokens(canonical_text)):
            continue
        metadata = chunk.metadata or {}
        body_tokens = _tokens("{} {}".format(chunk.section, chunk.content))
        metadata_tokens = _tokens("{} {}".format(
            metadata.get("topic") or "", " ".join(metadata.get("keywords") or [])
        ))
        body_overlap = query_tokens & body_tokens
        metadata_only_overlap = (query_tokens & metadata_tokens) - body_overlap
        weighted_overlap = len(body_overlap) + 0.2 * len(metadata_only_overlap)
        coverage = weighted_overlap / max(1, len(query_tokens))
        specificity = len(body_overlap) / max(1, min(len(body_tokens), 20))
        score = coverage * 0.8 + specificity * 0.2
        scored.append((score, chunk))
    rows = [
        {
            "source_id": chunk.source_id,
            "section": chunk.section,
            "content": chunk.content,
            "metadata": dict(chunk.metadata),
            "status": "approved",
            "active": True,
            "similarity": score,
        }
        for score, chunk in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
    ]
    selected_topics = {
        str((row.get("metadata") or {}).get("topic") or "") for row in rows
    }
    obligation_rows = [
        {"metadata": dict(chunk.metadata)}
        for chunk in chunks
        if str((chunk.metadata or {}).get("topic") or "") in selected_topics
    ]
    return build_knowledge_retrieval(
        rows,
        query=query,
        obligation_rows=obligation_rows,
        available_arguments=available_arguments,
    )
