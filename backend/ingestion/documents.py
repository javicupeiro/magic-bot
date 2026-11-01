"""Shared, JSON-serializable contract for retrievable rulebook documents."""

from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import quote

Kind = Literal["section", "rule", "glossary"]


def document_id(version: str, kind: Kind, identifier: str) -> str:
    """Build an edition-scoped ID without lossy lowercasing or slug conversion.

    Percent-encoding preserves punctuation and accents while protecting the
    colon separators between edition, kind and rule number or glossary term.
    """
    return f"mtg:{quote(version, safe='')}:{kind}:{quote(identifier, safe='')}"


@dataclass
class Reference:
    """Explicit source link; resolved becomes true only for one matching target."""

    kind: Kind
    identifier: str
    target_id: str
    resolved: bool = False


@dataclass
class Document:
    """Shared parser, ingestion and retrieval record for one source fragment.

    Pages are physical, one-based PDF pages. Optional identifiers describe the
    fragment's kind: glossary terms never receive invented rule numbers.
    """

    id: str
    kind: Kind
    text: str
    version: str
    page_start: int
    page_end: int
    title: str
    references: list[Reference] = field(default_factory=list)
    rule_number: str | None = None
    parent_id: str | None = None
    term: str | None = None
    section_number: str | None = None

    def to_dict(self) -> dict:
        """Convert this record and its nested references for JSONL serialization."""
        return asdict(self)
