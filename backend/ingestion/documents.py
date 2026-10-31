"""Shared, JSON-serializable contract for retrievable rulebook documents."""

from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import quote

Kind = Literal["section", "rule", "glossary"]


def document_id(version: str, kind: Kind, identifier: str) -> str:
    """Encode identifiers losslessly, so different terms never share a slug."""
    return f"mtg:{quote(version, safe='')}:{kind}:{quote(identifier, safe='')}"


@dataclass
class Reference:
    kind: Kind
    identifier: str
    target_id: str
    resolved: bool = False


@dataclass
class Document:
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
        return asdict(self)
