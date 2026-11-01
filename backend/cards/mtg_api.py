"""Bounded synchronous access to magicthegathering.io v1, isolated from callers.

The adapter fetches only requested pages, applies local filters and groups card
printings for presentation. It neither persists nor indexes a local catalogue.
The provider announces service through March 1, 2027; replacing this adapter
keeps HTTP details out of the CLI and future chatbot tools.
"""

from dataclasses import asdict
from datetime import date
import time

import httpx

from backend.cards.filters import CardFilters, color_symbols
from backend.cards.models import ApiError, Card, CatalogueResult

BASE_URL = "https://api.magicthegathering.io/v1"
TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def _group_cards(records: list[dict]) -> list[Card]:
    """Group by canonical English name, retaining full source records per printing."""
    grouped = {}
    for raw in records:
        # A name identifies a presentation group, not a provider-wide oracle ID.
        key = raw["name"].casefold()
        if key not in grouped:
            grouped[key] = Card(raw["name"], raw.get("manaCost"), raw.get("cmc"),
                                color_symbols(raw.get("colors", [])),
                                color_symbols(raw.get("colorIdentity", [])),
                                raw.get("text"), raw.get("imageUrl"))
        card = grouped[key]
        # Preserve the entire record: different editions can have different data.
        if raw not in card.printings:
            card.printings.append(raw)
        # Later printings may carry fields missing from the first one.
        if card.image_url is None:
            card.image_url = raw.get("imageUrl")
        if card.text is None:
            card.text = raw.get("text")
    return list(grouped.values())


class MtgApiClient:
    """Search cards, resolve verified names and list sets through one HTTP pool.

    Use as a context manager to close owned connections. Inject an httpx.Client
    with MockTransport in tests so the same lookup logic runs without the network.
    """

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 10,
                 retries: int = 1, page_size: int = 100, max_pages: int = 3):
        """Configure limits; retries counts extra attempts for each requested page.

        timeout applies to HTTP operations, not the whole lookup. max_pages and
        page_size bound the fetched data; local filters can leave fewer matches.
        Injected HTTP clients remain caller-owned and are not closed here.
        """
        if not 0 < timeout < float("inf"):
            raise ValueError("timeout must be positive and finite")
        for name, value, low, high in (("retries", retries, 0, 3),
                                       ("page_size", page_size, 1, 100),
                                       ("max_pages", max_pages, 1, 20)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()
        self.timeout, self.retries = timeout, retries
        self.page_size, self.max_pages = page_size, max_pages

    def close(self) -> None:
        """Release an internally created connection pool."""
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, resource: str, params: dict) -> tuple[httpx.Response | None, ApiError | None]:
        """Fetch one page, returning either a successful response or a typed failure."""
        for attempt in range(self.retries + 1):
            try:
                response = self._client.get(f"{BASE_URL}/{resource}", params=params, timeout=self.timeout)
                if response.is_success:
                    return response, None
                error = ApiError("http", f"Catalogue returned HTTP {response.status_code}", response.status_code)
                # The provider also uses 403 for rate limits; ordinary 403 is permanent.
                transient = (response.status_code in TRANSIENT_STATUS or
                             (response.status_code == 403 and "rate limit exceeded" in response.text.casefold()))
            except httpx.TimeoutException:
                error, transient = ApiError("timeout", "Catalogue request timed out"), True
            except (httpx.ConnectError, httpx.ReadError):
                error, transient = ApiError("network", "Catalogue connection failed"), True
            except httpx.RequestError:
                error, transient = ApiError("network", "Catalogue request failed"), False
            if not transient or attempt == self.retries:
                return None, error
            # Short exponential backoff; constructor limits keep retries bounded.
            time.sleep(0.25 * 2 ** attempt)
        raise AssertionError("Unreachable retry state")

    def _pages(self, resource: str, params: dict, filters: dict, predicate) -> CatalogueResult:
        """Collect bounded pages, filter raw records and retain incomplete-search state.

        predicate checks one printing or set. Apply it before grouping so the
        source records attached to a card are exactly those satisfying the query.
        """
        result = CatalogueResult(filters={"requested": filters, "api": params,
                                          "local": filters, "page_size": self.page_size,
                                          "max_pages": self.max_pages})
        records = []
        for page in range(1, self.max_pages + 1):
            response, error = self._request(resource, {**params, "page": page, "pageSize": self.page_size})
            if error is not None:
                result.error = error
                # A later failure does not discard earlier successful pages.
                result.partial = result.pages_fetched > 0
                break
            try:
                # HTTP success alone does not guarantee usable catalogue data.
                batch = response.json()[resource]
                if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
                    raise ValueError("Invalid result list")
                for item in batch:
                    if not isinstance(item.get("name"), str) or not item["name"]:
                        raise ValueError("Missing catalogue name")
                    if resource == "cards":
                        color_symbols(item.get("colors", []))
                        color_symbols(item.get("colorIdentity", []))
                matches = [item for item in batch if predicate(item)]
            except (ValueError, KeyError, TypeError, AttributeError):
                result.error = ApiError("invalid_response", "Invalid catalogue response")
                result.partial = result.pages_fetched > 0
                break
            records.extend(matches)
            result.pages_fetched += 1
            # Never follow provider-supplied URLs: only advance the bounded page number.
            total = response.headers.get("Total-Count", "")
            # Total-Count takes precedence over stale next links seen in live responses.
            if total.isdigit():
                more = page * self.page_size < int(total)
            elif "next" in response.links:
                more = True
            elif "Link" in response.headers:
                more = False
            else:
                # A full page without metadata may have successors. At the cap,
                # prefer a partial result over incorrectly claiming completeness.
                more = len(batch) >= self.page_size
            if not more:
                break
            result.partial = page == self.max_pages
        result.items = _group_cards(records) if resource == "cards" else records
        # Partial data may also have an error; callers must inspect both fields.
        result.status = ("partial" if result.partial else "error" if result.error else
                         "ok" if result.items else "empty")
        return result

    def search_cards(self, *, name: str | None = None, language: str | None = None,
                     subtype: str | None = None, color: str | None = None,
                     color_identity: str | None = None, cmc: str | int | float | None = None) -> CatalogueResult:
        """Search live printings, then group matching editions by canonical name.

        Example: search_cards(color='White', subtype='Warrior', cmc='<2').
        name is a substring; color and color_identity each test membership.
        """
        filters = CardFilters(name, language, subtype, color, color_identity, cmc)
        return self._pages("cards", filters.parameters(), asdict(filters), filters.matches)

    def get_card_by_name(self, name: str, *, language: str | None = None) -> CatalogueResult:
        """Resolve exact English or explicitly localized names, ignoring letter case.

        Example: get_card_by_name('Vanguardia de Adanto', language='Spanish').
        No spelling correction or guessed translation is attempted. A partial
        lookup cannot establish uniqueness even when only one card was found.
        """
        filters = CardFilters(name=name, language=language)
        if not name.strip():
            raise ValueError("name must not be empty")
        result = self._pages("cards", filters.parameters(), asdict(filters),
                             lambda card: filters.matches(card, exact_name=True))
        if not result.partial and not result.error and len(result.items) > 1:
            # Distinct English cards can share the same verified localized name.
            result.status = "ambiguous"
        return result

    def list_sets(self, *, name: str | None = None, released_from: str | None = None,
                  released_to: str | None = None) -> CatalogueResult:
        """Filter inclusive YYYY-MM-DD bounds locally and sort oldest first.

        Sets remain original provider dictionaries. Missing dates sort last and
        cannot satisfy a date bound. Sorting covers only the fetched pages.
        """
        for value in (released_from, released_to):
            if value is not None:
                date.fromisoformat(value)
                if len(value) != 10:
                    raise ValueError("Release dates must use YYYY-MM-DD")
        if released_from and released_to and released_from > released_to:
            raise ValueError("released_from must not exceed released_to")
        if name is not None and not name.strip():
            raise ValueError("name must not be empty")

        def matches(item):
            # Valid ISO date strings sort chronologically without date conversion.
            released = item.get("releaseDate")
            return ((name is None or name.strip().casefold() in item["name"].casefold()) and
                    (released_from is None or bool(released and released >= released_from)) and
                    (released_to is None or bool(released and released <= released_to)))

        result = self._pages("sets", {"name": name.strip()} if name else {},
                             {"name": name, "released_from": released_from, "released_to": released_to}, matches)
        result.items.sort(key=lambda item: (item.get("releaseDate") or "9999-99-99", item["name"]))
        return result
