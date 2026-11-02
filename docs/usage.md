# Command guide

Run from the repository root with `uv sync --locked`. Keep an existing `.env`;
otherwise create one from `.env.example`. Never put keys in command arguments.

## Choose the command for the layer you want to inspect

- `pdf-to-text`: extraction only, no LLM.
- `parse-rules`: TXT to structured rules, sections and glossary.
- `ingest-rules`: cached embeddings and Chroma ingestion.
- `search-rules`: vector/hybrid retrieval or exact lookup, no generated answer.
- `benchmark-retrieval`: measure retrieval against reviewed source IDs.
- `answer-rules`: simple bilingual RAG baseline without cards or graph routing.
- `card-catalogue`: direct HTTP client without a model.
- `card-chat`: only the card subgraph, using real model-selected tools.
- `magic-chat`: full routing, cards, rules and bounded answer review.

All commands support `--help`. Data/index defaults are relative to the repository
root. The current PDF default is `data/game_rules/MagicCompRules_20260417.pdf`;
use `pdf-to-text --input` for another filename.

## Main workflow

```sh
uv run --locked magic-chat '¿Qué es dañar primero?'
uv run --locked magic-chat 'What are the phases of a turn?'
uv run --locked magic-chat 'Find Lightning Bolt' --json
uv run --locked magic-chat 'Find Lightning Bolt' --events
uv run --locked magic-chat 'Can I use ninjutsu after first-strike damage?' -k 3
```

Each invocation starts a new stored conversation unless `--conversation-id` is
supplied. Read its ID from `Conversation:` in normal output, `conversation_id` in
`--json`, or the first `--events` object. Reuse the same SQLite file after restart:

```sh
uv run --locked magic-chat 'Busca criaturas elfo verdes.' --conversation-id elves
uv run --locked magic-chat '¿y que cueste uno?' --conversation-id elves
# A different ID starts with no previous filters.
uv run --locked magic-chat '¿y que cueste uno?' --conversation-id separate
```

`--memory-db` selects the SQLite file (default
`data/conversations/checkpoints.sqlite`). `--history-budget` limits serialized
recent messages plus the current question (default 8,000 characters), independently
of the evidence budget. Full history stays stored. See
[conversation memory](conversation-memory.md) for the Python lifecycle and limits.

Rules need the ingested index and embedding/model credentials. A card-only route
does not open Chroma. Options include `--db`, `--collection`, `--version`, `-k`,
`--max-calls`, `--max-pages`, `--context-budget`, `--method vector|hybrid`,
`--source` (matching JSONL) and `--candidate-limit` (default 20 per hybrid branch).
Vector remains the default after the measured comparison.
`search-rules` and `answer-rules` accept the same retrieval options;
`benchmark-retrieval --method both --output data/retrieval/local-comparison.json`
saves a paired comparison with source changes, recall, latency and context size.

Defaults: twenty vector hits per query and kind, four tool calls, three HTTP pages
per lookup, and 24,000 serialized evidence characters. Rejected tool requests count
toward the call budget. Up to ten calls may be configured. There is no overall
wall-clock deadline yet.

Rules answers now pass citation validation and model evidence review. A rejection
can spend one additional retrieval round (up to three distinct English queries,
300 characters each), repair the draft, then validate and review again. Persistent
rejection ends with abstention. `answer-rules` remains the unreviewed baseline.

Normal output prints the answer, conversation ID, main-node summaries, tool exchanges and citations.
Review summaries expose brief factual objections and the additional-round count.
Inspect `reviews`, `candidate_draft`, `validation_error`, `repair_queries` and
`additional_retrievals` in `--json`.
`--json` prints internal state for local debugging, including native messages.
`--events` instead prints real completed node names as the graph executes; it is
not a second execution and does not print the final answer. Do not publish raw
`--json` output as a frontend event stream.

## Card tools only

```sh
uv run --locked card-chat 'Find Lightning Bolt'
uv run --locked card-chat --trace 'Busca Warriors blancos con valor de maná menor que 2.'
uv run --locked card-chat --json 'Find Lightning Bolt'
```

`--trace` adds arguments, call IDs and statuses. `--json` includes the structured
reply and HTTP evidence. CLI options also include `--timeout`, `--retries` and
`--page-size`. A reported catalogue error is a completed tool result, not a claim
that no card exists. Inspect `status`, `partial` and `error` together.

```python
from backend.cards.agent import CardAgent
from backend.cards.mtg_api import MtgApiClient

with MtgApiClient(max_pages=3) as api:
    result = CardAgent(api, max_calls=4).answer("Find Lightning Bolt")
    print(result.reply.text)
```

This uses the same compiled card graph as `magic-chat`. It does not maintain a
parallel hand-written loop. Card names declared in final output must come from
complete successful results; sentences still require quality review.

## Configuration and outputs

Model settings are `OPENAI_LLM_MODEL` and `OPENAI_REASONING_EFFORT`. Embedding
settings are `OPENAI_EMBEDDING_MODEL` and `OPENAI_EMBEDDING_DIMENSIONS`. Query
embeddings use the configuration recorded in the index. Environment variables
have precedence over `.env`.

Langfuse tracing stays off unless both `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` are set. `LANGFUSE_TRACING_ENABLED=false` forces it off.
A tracing failure is printed to stderr and does not change the answer. See
[observability](observability.md).

Prompts record their versions and hashes. Claims refer to retrieved source IDs;
Python builds excerpts and citation markers from stored records. Invalid IDs
cause abstention, not fabricated citations. All current conversations are single
turns, despite the `chat` command names.

See [architecture](architecture.md), [ingestion](ingestion-and-retrieval.md),
[catalogue semantics](catalogue.md) and [prompt conventions](prompts.md).


## Retrieval parameter sweep

```sh
uv run --locked benchmark-retrieval --sweep --repeats 3 --output data/retrieval/parameter-sweep-local.json
```

The fixed sweep compares final k=5,8,10,20 for vector and hybrid, with hybrid
candidate limits 20,40,80 per branch (16 configurations). In sweep mode these
values replace `-k`, `--method` and `--candidate-limit`. Three rounds make 576
real embedding/search calls for the 12-case bank. `--repeats` changes the number
of rounds; the default is three. No query embedding cache is introduced.

The report retains full first-round sources/candidates, all repeated result IDs,
per-query metrics, randomized configuration order (seed 42), and aggregate recall,
MRR, hit/full-recall rates, latency percentiles and context sizes. Lexical corpus
validation/build is measured separately. This is a retrieval experiment, not a
measurement of generated answer quality or workflow prompt size.
