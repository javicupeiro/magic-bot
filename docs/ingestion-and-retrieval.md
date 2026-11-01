# Ingestion and retrieval

Run commands from the repository root after `uv sync --locked`.

The command reads `data/game_rules/MagicCompRules_20260417.pdf`, preserves the original, and writes:

- [data/processed/MagicCompRules_20260417.txt](../data/processed/MagicCompRules_20260417.txt): UTF-8 text with `\n\f\n` between pages. `\f` is the form feed character (U+000C). Splitting on this separator recovers pages in order, including empty pages. Rulebook TXT files are included in version control for use by the project.
- `data/processed/MagicCompRules_20260417.manifest.json`: PDF SHA-256, edition declared in the document, page count, extractor version, pypdf version, extraction mode, character counts per page, and warnings. Generated manifests remain ignored by Git.

Configurable paths:

```sh
uv run --locked pdf-to-text --input path/document.pdf --output data/processed/document.txt --manifest data/processed/document.manifest.json
```

You can also run `uv run --locked python -m backend.ingestion.pdf_to_text`. Without `--manifest`, the manifest uses the TXT filename with a `.manifest.json` extension. Default paths are relative to the current working directory. Outputs go in `data/processed/`, separate from the Python code. Git includes `MagicCompRules_*.txt` files in that directory; other generated outputs remain ignored.

In [pdf_to_text.py](../backend/ingestion/pdf_to_text.py), `main()` resolves paths and `extract_pdf()` reads the bytes, computes the hash, extracts each page, and writes the text and manifest. It does not normalize whitespace or correct content. Empty pages, unusually short pages, and pages containing suspicious characters are preserved and reported through stderr and the manifest. These checks are heuristics and cannot detect every extraction problem. Scanned PDFs need a separate workflow; this command reports missing text without attempting OCR.

Parse the extracted text:

```sh
uv run --locked parse-rules
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:rule:100.1a'
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:glossary:Abandon'
uv run --locked python -m unittest discover -s tests -v
```

The parser writes `data/processed/MagicCompRules_20260417.jsonl` and a `.report.json` summary. Both are reproducible generated files ignored by Git. Customize them with `--input`, `--output`, and `--report`. The edition comes from the source's effective-date declaration; `--version` overrides it or supplies a missing edition. `python -m backend.ingestion.parse_rules` provides the same CLI. `--id` regenerates the files and prints the requested document, failing if the ID is unknown.

The shared contract in [documents.py](../backend/ingestion/documents.py) has `id`, `kind`, `text`, `version`, one-based physical `page_start`/`page_end`, `title`, and `references`. Each rule or subrule has `rule_number` and `parent_id`; major sections have `kind: section` and `section_number`, so references such as “section 8” have a target. Glossary entries have `term` and a null `rule_number`. Inapplicable fields are null. Rule text excludes the identifying number; glossary text excludes the term heading. Examples and source line breaks remain in the body, including continuations across pages. For rules, the title is the text following the number on its first source line.

IDs follow `mtg:<edition>:<kind>:<identifier>`, with edition and identifier URL-encoded losslessly. They depend on the declared edition and rule number, section number, or exact glossary term, never page position. References contain `kind`, `identifier`, `target_id`, and `resolved`. Explicit rule/section lists and numeric or letter ranges are parsed deterministically, skipping the unused subrule letters l and o. Glossary “See” links are also extracted; exact terms resolve automatically, while aliases and ambiguous lists remain in the review report.

The report includes counts by kind, duplicate IDs, empty fragments, unresolved references, and unresolved parents. Duplicate IDs or empty fragments cause a nonzero exit after writing the report, leaving any existing JSONL untouched. Unresolved references produce a warning for review. No expected fragment count is hardcoded.

This parser targets the English Comprehensive Rules format: numbered rules and title-case glossary headings. It excludes the introduction, contents, credits, page numbers, and repeated rulebook/section/glossary headings. It joins lines only when extracting references; document bodies retain their line breaks. Other layouts or glossary capitalization conventions need parser changes and fixtures. It preserves extraction defects such as glued words instead of attempting to rewrite the source.


Create the persistent vector index:

```sh
# If .env does not exist, copy .env.example and fill in your key.
# Keep an existing .env unchanged.
uv run --locked parse-rules
uv run --locked ingest-rules --limit 10
uv run --locked ingest-rules
uv run --locked ingest-rules --limit 10
```

`OPENAI_API_KEY` authorizes uncached embedding requests. `OPENAI_EMBEDDING_MODEL`
defaults to `text-embedding-3-small`; `OPENAI_EMBEDDING_DIMENSIONS` defaults to
`1536`. The commands load `.env` without overriding existing environment variables.
Credentials are never included in ingestion reports. Ingestion and semantic search call OpenAI; exact retrieval does not.
Unit and integration tests use simulated embeddings.

`ingest-rules` (also `python -m backend.ingestion.ingest`) reads the existing JSONL
without rewriting it. Options: `--input`, `--db` (default `data/chroma`),
`--collection` (default `magic-rules`), `--batch-size` (default 64, range 1–256),
`--limit` (positive sample size), and `--rebuild` (delete and recreate only the
selected collection). Reingesting a sample upserts its IDs without removing other
records. To remove obsolete records, rebuild from the complete JSONL. Use one writer
at a time for each collection; concurrent ingestion/rebuild is not supported.

The index uses cosine distance. Each embedding input is the trimmed title,
`\n\n`, and trimmed body, consistently across sections, rules, and glossary.
IDs, edition, page positions, and references are excluded from the semantic text.
Chroma stores the original body as its document and the remaining shared contract
as metadata, together with semantic `content_hash`, complete-record `source_hash`,
model, and dimensions. Null metadata fields are omitted; `references` is a JSON
string preserving the structured list from the readable source JSONL.

The collection records its model, dimensions, semantic text format, schema,
Chroma version, and explicit HNSW configuration. Incompatible configuration fails
before embedding or upserting; choose a different collection name or explicitly
use `--rebuild`. Rebuild deletes that collection before ingesting, so a provider
failure can leave it partially populated. Completed batches can be resumed.
`data/chroma/embedding-cache.sqlite3` caches successful vectors by semantic content,
model, dimensions, and text format, independently of IDs and provenance. Repeated
content, metadata-only changes, and retries after failed Chroma writes reuse the
cache. A fully cached run needs no API key. A successful response interrupted before
the cache commit may need to be requested again.

Each run prints `embedded`, `reused`, and `collection_count`, then records the last
successful run in `data/chroma/<collection>.ingestion.json`, including editions,
input SHA-256, configuration, and completion time. `embedded` counts newly generated
unique vectors; `reused` counts input records served without a new vector request.
These local files remain ignored by Git.

Reopen the persisted collection in a fresh process and retrieve by exact ID:

```sh
uv run --locked python - <<'PYTHON'
import chromadb
from chromadb.config import Settings
from backend.ingestion.documents import document_id
client = chromadb.PersistentClient("data/chroma", settings=Settings(anonymized_telemetry=False))
collection = client.get_collection("magic-rules", embedding_function=None)
print(collection.count())
print(collection.get(ids=[document_id("April 17, 2026", "rule", "100.1a")]))
PYTHON
```

Run all checks with `uv run --locked python -m unittest discover -s tests -v`.
The ingestion integration checks use a temporary persistent Chroma database and
fake vectors, including reopening it from a separate Python process. Provider
argument tests mock the OpenAI client and never contact OpenAI.

The provider adapter follows the [official OpenAI embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create).


Search the indexed sources without generating an answer:

```sh
uv run --locked search-rules 'turn phases'
uv run --locked search-rules 'mana' -k 10
uv run --locked search-rules 'first strike' -k 3 --kind rule
uv run --locked search-rules 'ninjutsu' --kind glossary
uv run --locked search-rules --rule 702.49a
uv run --locked search-rules --id 'mtg:April%2017%2C%202026:rule:702.49a'
```

`search-rules` (also `python -m backend.retrieval.retrieval`) opens an existing collection;
run ingestion first. `--db` and `--collection` select the index. The query is
embedded with the model and dimensions recorded by ingestion, independent of
current embedding environment overrides. The stored schema, text format, Chroma
version and HNSW configuration must match the current ingestion contract before
searching. Queries are trimmed and embedded as plain query text; indexed sources
retain the ingestion title/body format. Semantic search needs `OPENAI_API_KEY`.
`-k` must be positive (default 5); requests exceeding the collection size are
capped. `--kind` accepts `rule`, `section` or `glossary`. An exact `--version`
filter selects an edition, for example `--version 'April 17, 2026'`.

JSON output includes ID, kind, title, full body, physical PDF page range, edition,
distance, similarity and parsed references. An unknown exact ID/rule or unmatched filter returns an
empty results list. `--rule` looks up the exact `rule_number` metadata, so `702.49a`
is distinct from its parent `702.49`. It returns matches across indexed editions
unless `--version` is supplied. `--id` looks up the complete edition-qualified ID
and also supports kind/edition filters. Both exact paths use Chroma `get`, need
no API key, and report null distance/similarity.

For pinned Chroma 1.5.5 and the cosine index, distance is
`d = 1 - dot(a, b) / (norm(a) * norm(b))`; smaller distances rank first, and
`similarity = 1 - d`. Distance is theoretically in `[0, 2]` and similarity in
`[-1, 1]`, with floating-point rounding possible. Neither is a probability or
confidence score. The formula is documented in the
[Chroma 1.5.5 distance implementation](https://github.com/chroma-core/chroma/blob/1.5.5/rust/distance/src/types.rs).
The integration fixture checks aligned, perpendicular and opposite vectors
through the public retrieval API, expecting distances 0, 1 and 2. Chroma handles
ranking; retrieval adds no threshold, reranking or answer generation. HNSW search
is approximate, and lexical ambiguity or short heading documents can outrank
useful explanatory rules.

[data/retrieval/queries.json](../data/retrieval/queries.json) contains eight English
queries and a non-exhaustive set of manually reviewed relevant source IDs with
reasons, selected from the April 17, 2026 edition before the initial run.
[data/retrieval/baseline.json](../data/retrieval/baseline.json) saves the initial top-5
results, including full text, pages, distances, configuration, collection size,
timestamp and reviewed-source recall per query. Generate a separate comparison:

```sh
uv run --locked benchmark-retrieval --output data/retrieval/comparison-k10.json -k 10
```

`benchmark-retrieval` also accepts `--cases`, `--db` and `--collection`. It verifies
that all reviewed sources exist under the selected edition and optional per-case
kind filter before embedding any query. The output path is explicit and is
replaced if it already exists. Keep the original baseline for comparisons;
rerunning requests query embeddings from OpenAI. Snapshots and source judgments
are versionable, while the vector database remains local and ignored by Git.
Recall here describes only the reviewed source set; other returned documents can
also be relevant. Missing sources are recorded observations, not failing tests.
In the initial top-5 run, `500.1` is absent for `turn phases`, `106.1` is absent for
`mana`, and `702.7b` is absent for `first strike`; these limitations are retained
in the baseline for future comparison.


Retrieval code lives in `backend/retrieval/retrieval.py`, which contains the
reusable API and CLI together, following the ingestion module pattern. Import
`Retriever` from `backend.retrieval.retrieval`.
`evals/retrieval.py` is a development tool for measuring retrieval quality against
reviewed sources; it runs through `benchmark-retrieval` or
`python -m evals.retrieval`. It uses the backend API and is separate from the
application runtime. Automated correctness checks live in `tests/`; evaluation
queries and saved results live in `data/retrieval/`.
