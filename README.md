# Magic bot

Local ingestion tools that extract the rulebook's existing text layer and parse rules and glossary entries into structured documents, then embed those documents into a local vector index. Extraction and parsing do not use OCR or language models.

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it). Run from the repository root:

```sh
uv sync --locked
uv run --locked pdf-to-text
```

Runtime dependencies include `pypdf`, the OpenAI SDK, `python-dotenv`, and Chroma pinned to version 1.5.5. Hatchling packages the `backend` directory for installation. Document ingestion code lives in `backend/ingestion/`.

The command reads `data/game_rules/MagicCompRules_20260417.pdf`, preserves the original, and writes:

- [data/processed/MagicCompRules_20260417.txt](data/processed/MagicCompRules_20260417.txt): UTF-8 text with `\n\f\n` between pages. `\f` is the form feed character (U+000C). Splitting on this separator recovers pages in order, including empty pages. Rulebook TXT files are included in version control for use by the project.
- `data/processed/MagicCompRules_20260417.manifest.json`: PDF SHA-256, edition declared in the document, page count, extractor version, pypdf version, extraction mode, character counts per page, and warnings. Generated manifests remain ignored by Git.

Configurable paths:

```sh
uv run --locked pdf-to-text --input path/document.pdf --output data/processed/document.txt --manifest data/processed/document.manifest.json
```

You can also run `uv run --locked python -m backend.ingestion.pdf_to_text`. Without `--manifest`, the manifest uses the TXT filename with a `.manifest.json` extension. Default paths are relative to the current working directory. Outputs go in `data/processed/`, separate from the Python code. Git includes `MagicCompRules_*.txt` files in that directory; other generated outputs remain ignored.

In [pdf_to_text.py](backend/ingestion/pdf_to_text.py), `main()` resolves paths and `extract_pdf()` reads the bytes, computes the hash, extracts each page, and writes the text and manifest. It does not normalize whitespace or correct content. Empty pages, unusually short pages, and pages containing suspicious characters are preserved and reported through stderr and the manifest. These checks are heuristics and cannot detect every extraction problem. Scanned PDFs need a separate workflow; this command reports missing text without attempting OCR.

Parse the extracted text:

```sh
uv run --locked parse-rules
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:rule:100.1a'
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:glossary:Abandon'
uv run --locked python -m unittest discover -s tests -v
```

The parser writes `data/processed/MagicCompRules_20260417.jsonl` and a `.report.json` summary. Both are reproducible generated files ignored by Git. Customize them with `--input`, `--output`, and `--report`. The edition comes from the source's effective-date declaration; `--version` overrides it or supplies a missing edition. `python -m backend.ingestion.parse_rules` provides the same CLI. `--id` regenerates the files and prints the requested document, failing if the ID is unknown.

The shared contract in [documents.py](backend/ingestion/documents.py) has `id`, `kind`, `text`, `version`, one-based physical `page_start`/`page_end`, `title`, and `references`. Each rule or subrule has `rule_number` and `parent_id`; major sections have `kind: section` and `section_number`, so references such as “section 8” have a target. Glossary entries have `term` and a null `rule_number`. Inapplicable fields are null. Rule text excludes the identifying number; glossary text excludes the term heading. Examples and source line breaks remain in the body, including continuations across pages. For rules, the title is the text following the number on its first source line.

IDs follow `mtg:<edition>:<kind>:<identifier>`, with edition and identifier URL-encoded losslessly. They depend on the declared edition and rule number, section number, or exact glossary term, never page position. References contain `kind`, `identifier`, `target_id`, and `resolved`. Explicit rule/section lists and numeric or letter ranges are parsed deterministically, skipping the unused subrule letters l and o. Glossary “See” links are also extracted; exact terms resolve automatically, while aliases and ambiguous lists remain in the review report.

The report includes counts by kind, duplicate IDs, empty fragments, unresolved references, and unresolved parents. Duplicate IDs or empty fragments cause a nonzero exit after writing the report, leaving any existing JSONL untouched. Unresolved references produce a warning for review. No expected fragment count is hardcoded.

This parser targets the English Comprehensive Rules format: numbered rules and title-case glossary headings. It excludes the introduction, contents, credits, page numbers, and repeated rulebook/section/glossary headings. It joins lines only when extracting references; document bodies retain their line breaks. Other layouts or glossary capitalization conventions need parser changes and fixtures. It preserves extraction defects such as glued words instead of attempting to rewrite the source.


Create the persistent vector index:

```sh
cp .env.example .env  # Fill in your key if .env does not already exist.
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
distance and similarity. An unknown exact ID/rule or unmatched filter returns an
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

[data/retrieval/queries.json](data/retrieval/queries.json) contains eight English
queries and a non-exhaustive set of manually reviewed relevant source IDs with
reasons, selected from the April 17, 2026 edition before the initial run.
[data/retrieval/baseline.json](data/retrieval/baseline.json) saves the initial top-5
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

Generate a grounded answer in English or Spanish:

```sh
uv run --locked answer-rules '¿Cuáles son las fases de un turno?' --version 'April 17, 2026'
uv run --locked answer-rules 'What are the phases of a turn?'
uv run --locked answer-rules '¿Cuándo se vacía el maná no usado?'
uv run --locked answer-rules 'When does unused mana empty?'
uv run --locked answer-rules '¿Qué es ninjutsu?'
uv run --locked answer-rules 'What is ninjutsu?'
uv run --locked answer-rules '¿Cuál es la capital de Francia?'
```

`answer-rules` (also `python -m backend.generation.answers`) needs an ingested index and
`OPENAI_API_KEY`. Both normalization and generation use the Responses API with
structured Pydantic outputs, following the
[official Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
`OPENAI_LLM_MODEL` defaults to `gpt-6-luna`; `OPENAI_REASONING_EFFORT` defaults to
`high`. All three settings support `.env`, and existing environment variables
take precedence. No `temperature` or `top_p` is sent. LLM configuration is
independent of the embedding configuration stored in the index.

The service preserves the original question exactly and detects its language
(`en` or `es`). Normalization prepares a faithful English query, preserving names,
quantities, rule numbers and conditions. Retrieval searches rules and glossary
separately, with up to `-k` results **per kind** (default 5, up to 10 total).
`--db`, `--collection` and `--version` work as in `search-rules`. Use `--version`
to restrict evidence to one edition when multiple editions are indexed.

JSON output contains `original_question`, `language`, `english_query`,
`can_answer`, `answer`, per-paragraph `claims` with their `source_ids`, necessary
`assumptions`, and `citations`. Every citation contains its stored source ID,
kind, title, full source text as `excerpt`, physical page range and edition.
`retrieved_sources` includes the full context for inspection, including sources
the answer did not cite. `prompts` records each prompt's name, version and SHA-256;
`model` and `reasoning_effort` identify the generation configuration.

The model selects supporting IDs; Python validates that every claim has a source
in the retrieved context and builds citation markers and excerpts from stored
records. Unknown IDs, empty evidence, model-written citation markers and malformed
answer contracts cause abstention. These checks validate provenance; semantic
support and faithful translation still depend on the model and need manual review.
There is no external knowledge, card lookup, history, relevance threshold or judge.

With empty or insufficient context, `can_answer` is false, the response contains
a brief abstention in the original language, and claims, assumptions and citations
are empty. `abstention_reason` distinguishes `empty_context`,
`insufficient_evidence`, `invalid_citations_or_answer` and
`unavailable_structured_answer`. A refusal or incomplete generation also abstains.
Normalization failures and provider/transport errors exit the CLI with an error;
they do not produce factual answers. An unrelated question such as the capital of
France must abstain even if the search returns superficially similar records.

Prompts live in `backend/generation/prompts/normalize/v1.md` and
`backend/generation/prompts/answer/v1.md`; no prompt text is embedded in Python. Each file
starts with `Name`, `Version` and comma-separated `Variables` headers, followed
by a blank line and its English instructions. `backend.generation.prompt_loader.load_prompt`
checks the requested name/version and exact `$variable` contract, hashes the
original file bytes and reports missing files explicitly. Rendering rejects both
missing and unexpected variables; values are substituted once, so user-supplied
dollar signs remain data. New prompt versions should be new files. The package
includes the Markdown files for installed CLI use.

Backend modules are grouped by functionality: `backend/ingestion/` prepares and
indexes documents, `backend/retrieval/` searches stored sources, and
`backend/generation/` handles bilingual answers, the model adapter and versioned
prompts. Keep new runtime modules within the appropriate functional package.

Reusable API: `AnswerService(Retriever()).answer(question, k=5, version=None)`
from `backend.generation.answers`. The result is a Pydantic `Answer`; use `model_dump()` or
`model_dump_json()` to serialize it. Automated checks in `tests/test_answers.py`
mock model calls and cover prompt contracts, environment loading, Responses API
arguments, citation validation, both language paths and abstention behavior.
