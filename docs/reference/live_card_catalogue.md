# Live card catalogue

`backend/cards/mtg_api.py` isolates access to `https://api.magicthegathering.io/v1`
through `httpx`. It does not use an LLM or download, persist or index a catalogue.
The [provider documentation](https://docs.magicthegathering.io/) announces that
this API is deprecated and will function through **March 1, 2027**. Replace this
adapter before that date; coverage and localized data depend on the provider.

```sh
uv sync --locked
uv run --locked card-catalogue --max-pages 6 search --color White --subtype Warrior --cmc '<2'
uv run --locked card-catalogue name 'Adanto Vanguard'
uv run --locked card-catalogue name 'Lightning Bolt'
uv run --locked card-catalogue name 'Vanguardia de Adanto' --language Spanish
uv run --locked card-catalogue sets --name Khans --released-from 2014-01-01 --released-to 2014-12-31
uv run --locked card-catalogue --page-size 1 --max-pages 1 search --color W --subtype Warrior --cmc '<2'
```

`python -m backend.cards.cli` exposes the same commands. Global options precede
`search`, `name` or `sets`. JSON output includes `items`, `status`, `partial`,
`pages_fetched`, requested/API/local `filters`, and an optional `error`.
Exit codes: 0 for a complete lookup (including an empty list), 1 for a provider
failure, and 2 for a partial or ambiguous lookup or invalid command arguments.

Reusable API:

```python
from backend.cards.mtg_api import MtgApiClient

with MtgApiClient(max_pages=6, page_size=100, timeout=10, retries=1) as api:
    cards = api.search_cards(color="White", subtype="Warrior", cmc="<2")
    localized = api.get_card_by_name("Vanguardia de Adanto", language="Spanish")
    sets = api.list_sets(name="Khans", released_from="2014-01-01", released_to="2014-12-31")
    print(cards.to_dict())
```

`cmc` means mana value, separate from the printed symbolic `mana_cost`.
It accepts exact values (including numeric/string `0` and `=2`), `<2` and `<=2`.
Only integer equality is sent upstream, using integer notation because the live
server rejects `0.0`. Inequalities and fractional equality are evaluated on each
printing in Python before grouping. Missing mana values do not satisfy a cost filter.
`color` and `color_identity` each accept a single W/U/B/R/G symbol or English color
name. They test membership, so White can include multicolor cards. Color checks
`colors`; identity checks `colorIdentity` locally. Identity includes colors from
rules text and differs from a card's color; this is not a Commander legality test.
Both fields are normalized to symbols. The live provider requires symbols in the
`colors` request parameter, even though older documentation shows color names.

`search_cards(name=...)` uses substring matching. `get_card_by_name` requires an
exact case-insensitive English name, or a verified `foreignNames` match for an
explicit provider language such as `Spanish`. It groups English canonical names
and reports `ambiguous` if a complete localized lookup matches multiple cards.
It never invents translations or corrects spelling. Missing localized entries can
produce a complete empty result without proving that the card has no translation.

Each `Card` has normalized presentation fields and `printings`, containing every
distinct original matching API record, including its ID, set, localized names,
texts and image URLs when available. Reprints are grouped by canonical English
name; this is not an oracle-ID equivalence guarantee. Presentation text/image
fall back to a later matching printing if absent from the first. Missing images
remain null. Sets retain their original records and sort by release date ascending,
with unknown dates last; inclusive date bounds exclude unknown dates. Sorting and
date filtering apply only to pages actually fetched.

Lookups default to 3 pages of 100 records, 10 seconds per HTTP operation and one
retry. Limits are explicit: 1–20 pages, 1–100 records/page, 0–3 retries. Pagination
uses `Total-Count`, then `Link`, then page length; the total takes precedence over
stale next links observed on the live API. Full pages with no completion metadata
are conservatively partial at the limit. A page cap can leave `items` empty and
`partial=true`: that does not establish absence. Retained earlier pages remain
available if a later request fails. A partial name lookup does not establish a
unique resolution. Inspect both `partial` and `error` before using any result.

Errors distinguish `timeout`, `network`, `http` (including its status code) and
`invalid_response`. Retries use bounded exponential delays for timeouts,
connection/read failures, HTTP 429/500/502/503/504, and the provider's explicit
403 `Rate Limit Exceeded` response. Other HTTP errors and malformed responses are
not retried. There is no whole-lookup wall-clock deadline. Tests use
`httpx.MockTransport`; automatic tests never call the live catalogue:

```sh
uv run --locked python -m unittest discover -s tests -p test_mtg_api.py -v
```
