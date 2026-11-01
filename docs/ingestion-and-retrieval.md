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
`-k` must be positive (default 20); requests exceeding the collection size are
capped. `--kind` accepts `rule`, `section` or `glossary`. An exact `--version`
filter selects an edition, for example `--version 'April 17, 2026'`.

JSON output includes ID, kind, title, full body, physical PDF page range, edition,
distance, similarity, parsed references and retrieval method/rank provenance. An unknown exact ID/rule or unmatched filter returns an
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
vector ranking; vector mode adds no threshold, reranking or answer generation. HNSW search
is approximate, and lexical ambiguity or short heading documents can outrank
useful explanatory rules.

[data/retrieval/queries.json](../data/retrieval/queries.json) contains twelve English/Spanish
queries and a non-exhaustive set of manually reviewed relevant source IDs with
reasons from the April 17, 2026 edition. The original eight cases preceded the
initial baseline; four additional cases preceded the hybrid experiment.
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
`Retriever` from `backend.retrieval.retrieval`. `benchmark-retrieval` measures
reviewed-source coverage through `backend/retrieval/benchmark.py` and does not
generate answers. Automated correctness checks live in `tests/`; reviewed queries
and saved results live in `data/retrieval/`.


## Hybrid retrieval and the measured default

`Retriever(method="hybrid", source_path=Path("data/processed/MagicCompRules_20260417.jsonl"))`
selects the optional method for every caller, including the existing workflow.
An individual `search(..., method="vector")` can override it. The default remains
`vector`, based on the comparison below. `get()` and `rule()` always use exact
Chroma lookups; even with hybrid configured they need neither JSONL nor embeddings.

```sh
uv run --locked search-rules 'turn phases' --method vector
uv run --locked search-rules 'turn phases' --method hybrid
uv run --locked search-rules 'What does rule 702.49a say about ninjutsu?' --method hybrid
uv run --locked search-rules --rule 702.49a --method hybrid
uv run --locked magic-chat '¿Qué es dañar primero?' --method hybrid --json
uv run --locked answer-rules 'What is ninjutsu?' --method hybrid
uv run --locked benchmark-retrieval --method both -k 5 --output data/retrieval/local-comparison.json
```

`--source` selects the parsed JSONL and `--candidate-limit` selects the branch
candidate budget (default 20). These options are shared by the search, benchmark,
workflow and baseline-answer commands. A custom collection needs its corresponding
JSONL. Partial ingestion cannot be compared against a complete JSONL: finish
indexing first, or supply the exact matching corpus. Nothing is reindexed by search.

Data flows as follows:

1. The first hybrid query reads JSONL through ingestion's existing validator.
   `_lexical_index()` compares the complete ID sets, editions and `source_hash`
   values against Chroma, including metadata such as pages and references.
   A mismatch fails before requesting a query embedding. With `--version`, only
   that edition is compared/indexed; otherwise the entire corpus must match.
2. `BM25Index` in `backend/retrieval/hybrid.py` creates in-memory postings and
   lengths from the same title/body input plus the rule number. Unicode words are
   case-folded and dotted identifiers such as `702.49a` stay whole. It applies
   BM25 with k1=1.5, b=0.75 and positive log IDF, using the selected corpus for
   document frequencies. There is no stemming, stop-word removal or translation.
3. The vector branch embeds the query using the saved index configuration.
   Both branches filter kind/edition before selecting up to
   `max(k, candidate_limit)` hits. BM25 excludes documents without matching terms;
   tied lexical scores sort by ID. Chroma retains its approximate vector order.
4. `reciprocal_rank_fusion()` deduplicates IDs, adds one contribution per method
   and sorts the union. Positions start at **1**; each contribution is
   `1 / (60 + position)`. Equal fusion scores sort by ID. Only the top k survive.
5. Source content always comes back from Chroma as `SearchResult`. A lexical-only
   winner has null distance/similarity; other hits keep their vector distance.
   Hybrid order follows `fusion_score`, not distance. `ranks` records positions in
   each branch and `retrieval_method` records the selected mode. The existing
   workflow then deduplicates, bounds context and follows exact references as usual.

For example, vector `[A, B]` and BM25 `[B, C]` give B
`1/62 + 1/61 = 0.03252`, A `1/61 = 0.01639`, and C
`1/62 = 0.01613`. The fused order is `[B, A, C]`. No raw BM25 score is added
to cosine similarity. RRF is agreement between rankings, not confidence.

The lexical index is cached in memory per edition in one Retriever; there is no
second database, disk index or new dependency. Do not ingest/rebuild concurrently
with a live Retriever: reopen it after source/index changes. `last_search` retains
all candidate IDs, branch positions and fusion scores for the most recent search,
including candidates discarded from top k. Search CLI output and every benchmark
query save this diagnostic data; workflow sources retain their own ranks.

### Paired experiment

The twelve judgments in `data/retrieval/queries.json` were defined before the
experiment. They retain the original eight questions and add a Spanish turn-phase
question, a Spanish first-strike question, a summoning-sickness paraphrase and a
query mentioning `702.49a`. The Spanish questions form pairs with existing English
cases and go directly to retrieval. The workflow normally normalizes them to
English first, so these direct retrieval failures are not workflow answer scores.

The predefined promotion policy requires at least +0.05 mean reviewed-source
recall@5, no individual regression, at most 25% mean serialized-context growth,
and at most 25% mean search-latency growth. This is a small exploratory bank, not
a comprehensive relevance judgment or a statistical performance study.

`data/retrieval/hybrid-comparison.json` saves the live paired run over all **4,020**
records of the April 17, 2026 edition, using `text-embedding-3-small`, 1,536
dimensions, k=5, 20 candidates per hybrid branch and RRF constant 60. It includes
full sources, reviewed matches, configuration, branch ranks, all candidates and
added/removed IDs per query. `baseline.json` remains the original eight-case run.

The saved run measured mean search latency of **258.12 ms vector / 259.16 ms
hybrid**, and mean serialized context size of **3,588.58 / 3,715.17 characters**
(+3.53%). Mean title/body size was 1,499.75 / 1,465.08 characters. The first query
took 483.90 / 438.50 ms respectively; only the hybrid value includes lexical setup.
These times are wall-clock observations of this run, not a speed guarantee.

The mean reviewed-source recall is **0.76389 for both methods**. Hybrid recovers
`rule:500.1` for `turn phases` (0 → 1), but removes `glossary:Mana` for `mana`
(0.5 → 0), and removes `rule:405.1` for the stack-order question (1 → 0.5).
The JSON report lists every changed source and rank, including changes where
reviewed recall stays the same. Some replacement sources may be relevant despite
not being in the reviewed set; those are not automatically false positives.

Both methods still miss `rule:106.1` for `mana`, `rule:702.7b` for `first strike`,
and all three reviewed sources for the Spanish first-strike question. In that
Spanish case lexical overlap can even promote irrelevant symbols such as Y.
BM25 over an English corpus does not supply translation or general typo tolerance.
The casting question retains its reviewed source but hybrid adds long rules,
increasing that question's context substantially. Context limits still matter.

**Decision: retain vector as default and preserve hybrid as an explicit option.**
Equal mean recall and two regressions already fail the promotion policy, regardless
of timing. Latency includes embedding HTTP and network jitter; vector runs first,
so connection/provider warmup makes a single timing comparison inconclusive.
The first hybrid query also includes JSONL validation and BM25 construction.
Context characters count serialized SearchResult arrays (including provenance),
not tokens or the workflow's expanded, budgeted prompt. The report additionally
records title/body character counts. No generated-answer quality was evaluated.


### Varying result counts and candidate budgets

The follow-up [parameter sweep](retrieval-parameter-sweep.md) tests k=5,8,10,20
and hybrid candidate limits 20,40,80, with three rounds per configuration (576
fresh embedding/search calls). Saved measurements are in
[data/retrieval/parameter-sweep.json](../data/retrieval/parameter-sweep.json), with
a [portable interactive comparison](../data/retrieval/parameter-sweep.html).

The original k=5 result is limited to that configuration: vector recall rises
from 76.39% to 94.44% at k=20, while mean serialized context grows roughly fourfold.
Hybrid k=10/c20 reaches 87.50% with roughly half the context of vector k=20.
Increasing hybrid candidates can reduce recall: at k=10, c20/c40/c80 yield
87.50%/79.17%/83.33%. More branch candidates can gain a second RRF contribution
and displace a relevant vector-only hit. The guide records exact examples,
per-query recall, hit/full-recall rates, MRR, latency percentiles and context size.

This sweep excludes separately measured lexical setup from search latency and
shuffles configuration order each round. Timings therefore use a different
protocol from the original paired run. After reviewing the sweep, the application
default is now vector with k=20, matching the highest measured recall. The workflow
still shares its 24,000-character budget across rules and cards. No generated
answers or workflow budget survival were evaluated; all context sizes are raw
retrieval arrays rather than expanded prompts.
