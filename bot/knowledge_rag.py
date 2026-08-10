"""Versioned, reviewable knowledge retrieval for Fred.

Knowledge answers are useful for general policies and how the business works.
They are deliberately separated from the product catalog: neither source is
allowed to establish live stock, price, payment details, address or order
status.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


KNOWLEDGE_CHUNK_CHARS = 900
KNOWLEDGE_CHUNK_OVERLAP = 120
DEFAULT_MIN_KNOWLEDGE_SIMILARITY = 0.68


@dataclass(frozen=True)
class KnowledgeChunk:
    source_id: str
    section: str
    content: str


def _split_long_text(text: str) -> List[str]:
    """Split at words while retaining a small overlap for retrieval context."""
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


def chunk_markdown(source_id: str, markdown: str) -> List[KnowledgeChunk]:
    """Chunk a Markdown source by headings, then by bounded word windows."""
    sections: List[tuple[str, List[str]]] = []
    heading = "Información general"
    lines: List[str] = []

    def flush() -> None:
        if lines:
            sections.append((heading, list(lines)))
            lines.clear()

    for line in markdown.splitlines():
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
            chunks.append(KnowledgeChunk(source_id, section, content))
    return chunks


def load_knowledge_chunks(directory: str | Path) -> List[KnowledgeChunk]:
    """Read only curated Markdown files; hidden files are never knowledge."""
    chunks: List[KnowledgeChunk] = []
    for path in sorted(Path(directory).glob("*.md")):
        if path.name.startswith("."):
            continue
        chunks.extend(chunk_markdown(path.stem, path.read_text(encoding="utf-8")))
    return chunks


def approved_knowledge_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    limit: int,
    min_similarity: float = DEFAULT_MIN_KNOWLEDGE_SIMILARITY,
) -> List[Dict[str, Any]]:
    """Keep only approved, sufficiently relevant rows from pgvector."""
    accepted = [
        row for row in rows
        if row.get("status") == "approved"
        and bool(row.get("active", True))
        and float(row.get("similarity") or 0) >= min_similarity
    ]
    return sorted(accepted, key=lambda row: -float(row["similarity"]))[:limit]


def format_knowledge_context(rows: Sequence[Dict[str, Any]]) -> str:
    """Bounded, attributed context passed to the LLM as a reference only."""
    if not rows:
        return ""

    lines = ["Conocimiento aprobado recuperado (no reemplaza datos vigentes):"]
    for row in rows:
        source = row.get("source_id") or "fuente interna"
        section = row.get("section") or "sin sección"
        content = " ".join(str(row.get("content") or "").split())
        lines.append("- [{} / {}] {}".format(source, section, content))
    lines.append(
        "Si falta un dato o afecta precio, stock, pago, dirección, plazo u orden, verificá con Tiendanube o Isa."
    )
    return "\n".join(lines)
