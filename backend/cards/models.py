"""Presentation records retain every original printing for source attribution."""

from dataclasses import asdict, dataclass, field


@dataclass
class Card:
    """One presentation card grouped from matching printings by English name.

    Optional fields stay None when the provider omits them. Original API records
    in printings preserve each edition's IDs, texts, images and localized names.
    """

    name: str
    # Symbolic printed cost (e.g. {1}{W}) differs from numeric mana value (2).
    mana_cost: str | None
    cmc: float | None
    colors: list[str]
    color_identity: list[str]
    text: str | None
    image_url: str | None
    printings: list[dict] = field(default_factory=list)


@dataclass
class ApiError:
    """Machine-readable failure category plus a safe technical explanation.

    kind is timeout, network, http or invalid_response. Only HTTP failures carry
    a status_code; a failed request is never represented as an empty catalogue.
    """

    kind: str
    message: str
    status_code: int | None = None


@dataclass
class CatalogueResult:
    """Cards or raw set records together with the completeness of their lookup.

    status is ok, empty, partial, ambiguous or error. Always inspect partial and
    error: a later page can fail while items still contains earlier matches.
    """

    items: list[Card | dict] = field(default_factory=list)
    status: str = "empty"
    partial: bool = False
    pages_fetched: int = 0  # Successfully decoded and filtered pages, not attempts.
    # Records requested filters, remote parameters, local checks and page limits.
    filters: dict = field(default_factory=dict)
    error: ApiError | None = None

    def to_dict(self) -> dict:
        """Recursively convert nested dataclasses for JSON output and future callers."""
        return asdict(self)
