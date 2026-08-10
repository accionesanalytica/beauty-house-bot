"""Small, deterministic helpers for catalog retrieval.

Catalog embeddings are an *identity* index: they help Fred find a likely
product or variant, but they are never proof of current price or stock. Those
facts still come from Tiendanube tools at reply time.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple


DEFAULT_MIN_SEMANTIC_SIMILARITY = 0.62
MAX_LEXICAL_TERMS = 4
_STOP_WORDS = {
    "con", "como", "para", "por", "que", "quiero", "tener", "tienen",
    "tenes", "info", "sobre", "unos", "unas", "del", "las", "los",
    "una", "uno", "the", "and",
    # Conversational filler must never become an AND filter.  For example,
    # "mmm no tenés nada color chocolate" should retrieve "chocolate", not
    # require a product name to contain "mmm", "nada" and "color" too.
    "mmm", "nada", "algo", "color", "hay", "disponible", "disponibles",
    "busco", "buscar", "necesito", "momento", "general", "todos", "todo",
    "dia", "dias", "pestanas", "pestana",
}


def normalize_text(value: Any) -> str:
    """Lowercase and remove accents for deterministic text comparisons."""
    value = unicodedata.normalize("NFD", str(value or ""))
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", value.lower()).strip()


def lexical_terms(query: str) -> List[str]:
    """Useful product-identifying terms, bounded to keep SQL work predictable."""
    terms: List[str] = []
    for term in re.findall(r"[a-z0-9]+", normalize_text(query)):
        if len(term) < 3 or term in _STOP_WORDS or term in terms:
            continue
        terms.append(term)
        if len(terms) == MAX_LEXICAL_TERMS:
            break
    return terms


def build_catalog_content(row: Dict[str, Any]) -> str:
    """Return stable product identity text for an embedding, never commercial state."""
    parts = [str(row.get("product_name") or "").strip()]
    optional_fields = (
        ("Variante", "variant_values"),
        ("SKU", "sku"),
        ("Código", "barcode"),
        ("Handle", "handle"),
        # Future catalog exports can provide these without changing the indexer.
        ("Marca", "brand"),
        ("Categoría", "category"),
        ("Descripción", "description"),
    )
    for label, field in optional_fields:
        value = str(row.get(field) or "").strip()
        if value:
            parts.append("{}: {}".format(label, value))
    return ". ".join(part for part in parts if part)


def lexical_catalog_query(query: str, limit: int) -> Tuple[str, Tuple[Any, ...]] | None:
    """Safe SQL for exact-ish identity matches before semantic retrieval.

    Every meaningful term must appear in the product identity fields. This is
    intentionally a narrow candidate search, not a replacement for vector
    retrieval. The values stay parameterized.
    """
    terms = lexical_terms(query)
    if not terms:
        return None

    searchable = (
        "LOWER(COALESCE(product_name, '') || ' ' || COALESCE(variant, '') "
        "|| ' ' || COALESCE(sku, ''))"
    )
    clauses = ["{} LIKE %s".format(searchable) for _ in terms]
    sql = """
        SELECT product_id, variant_id, sku, product_name, variant, 1.0 AS similarity
        FROM product_embeddings
        WHERE published = true AND {clauses}
        ORDER BY product_name, variant
        LIMIT %s
    """.format(clauses=" AND ".join(clauses))
    return sql, tuple(["%{}%".format(term) for term in terms] + [limit])


@dataclass(frozen=True)
class CatalogCandidate:
    product_id: Any
    variant_id: Any
    sku: str
    product_name: str
    variant: str
    semantic_similarity: float
    source: str


def _candidate_from_row(row: Dict[str, Any], source: str) -> CatalogCandidate:
    return CatalogCandidate(
        product_id=row.get("product_id"),
        variant_id=row.get("variant_id"),
        sku=str(row.get("sku") or ""),
        product_name=str(row.get("product_name") or ""),
        variant=str(row.get("variant") or ""),
        semantic_similarity=float(row.get("similarity") or 0),
        source=source,
    )


def fuse_catalog_candidates(
    lexical_rows: Iterable[Dict[str, Any]],
    semantic_rows: Iterable[Dict[str, Any]],
    *,
    limit: int,
    min_semantic_similarity: float = DEFAULT_MIN_SEMANTIC_SIMILARITY,
) -> List[CatalogCandidate]:
    """Prefer lexical identity hits, then only sufficiently similar vectors."""
    candidates: Dict[Any, CatalogCandidate] = {}

    for row in lexical_rows:
        candidate = _candidate_from_row(row, "lexical")
        candidates[candidate.variant_id] = candidate

    for row in semantic_rows:
        candidate = _candidate_from_row(row, "semantic")
        if candidate.semantic_similarity < min_semantic_similarity:
            continue
        existing = candidates.get(candidate.variant_id)
        if existing is None:
            candidates[candidate.variant_id] = candidate
        elif candidate.semantic_similarity > existing.semantic_similarity:
            candidates[candidate.variant_id] = CatalogCandidate(
                **{**existing.__dict__, "semantic_similarity": candidate.semantic_similarity}
            )

    ordered = sorted(
        candidates.values(),
        key=lambda item: (item.source != "lexical", -item.semantic_similarity, item.product_name),
    )
    return ordered[:limit]


def format_catalog_context(candidates: Sequence[CatalogCandidate]) -> str:
    """Context passed to the model with an explicit freshness boundary."""
    if not candidates:
        return ""

    lines = [
        "Candidatas del catálogo (identidad, no confirma stock ni precio):"
    ]
    for item in candidates:
        lines.append(
            "- product_id: {}; variant_id: {}; {}; SKU: {}; Variante: {}".format(
                item.product_id,
                item.variant_id,
                item.product_name,
                item.sku or "N/A",
                item.variant or "default",
            )
        )
    lines.append(
        "Antes de afirmar disponibilidad, precio o link, verificá la variante con una herramienta de Tiendanube."
    )
    return "\n".join(lines)
