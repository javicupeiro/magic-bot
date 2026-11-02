# Command guide

Run from the repository root with `uv sync --locked`. Keep an existing `.env`;
otherwise create one from `.env.example`. Never put keys in command arguments.

## Web chat and execution panel

```sh
uv sync --locked
uv run --locked uvicorn backend.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

Open **http://127.0.0.1:8000/ui/**. Run from the repository checkout; FastAPI
serves `frontend/` at `/ui/` and the API from the same origin. There is no npm
install, JavaScript build, permissive CORS or second UI service. The API welcome
page remains at `/`, interactive help at `/docs`. The Python-only wheel does not
bundle the UI assets. Keep an existing `.env`; credentials stay on the backend.
Rules questions still require the prepared Chroma index and provider credentials;
card lookups use the live catalogue. Real turns can incur model/embedding charges.

The left panel contains your messages, the final answer, expandable citations
(original excerpt, physical pages, source ID, kind and rules version) and the
actual card records returned by the API. Available HTTP(S) images are loaded
without a referrer; absent, unsafe or broken image URLs leave a text fallback.
All external prose is rendered as literal text, including HTML-looking strings.

The right panel shows **observed execution events for the latest turn**. Each
occurrence has its own step ID: repeated `cards.model`, `cards.tools`,
`cards.collect` and `cards.finish` steps remain separate. The enclosing `cards`
step is also visible. Review, validation, extra retrieval and repair appear only
when their backend events arrive. A card search ends with its results. Rules
retrieval appears only when the backend continues an interaction. `Turn finished`
means a terminal answer arrived; clarification and abstention also finish turns,
so that label does not assert factual approval.

Total duration is the terminal event's wall time. It is **not** the sum of step
durations: a parent overlaps its children. Tokens sum only `total_tokens` from
individual `model_usage` events, without counting parent spans. The total may be
partial; per-call input/output/total counts remain visible. Unknown values display
`Not available`, whereas reported zero tokens or zero duration display as zero.
The [HTTP contract](http-api.md) deliberately omits tool arguments, subqueries,
review verdict contents, model identifiers and trace URLs. The panel labels their
absence instead of reconstructing them, requesting another explanation from a
model, or exposing internal state. Tracing remains available through the
[backend's Langfuse setup](observability.md).

Use **New conversation** to create an isolated conversation. The selector lists
registered HTTP conversation IDs; **Reload history** fetches completed messages.
The last selected ID is kept in browser local storage and reopened on page load.
SQLite owns persisted history. Historical HTTP messages contain only role/text:
structured citations, card records and past execution events are not replayed.
The execution panel resets on history reload/switch so old evidence cannot be
mistaken for a new execution. CLI-only IDs are not listed automatically.

During a turn, sending, switching and history controls are disabled. Enter sends;
Shift+Enter inserts a newline. Tab/Shift+Tab navigate controls, Enter/Space toggle
citation disclosures, and focus is visibly outlined. Desktop uses two columns;
small screens stack chat above execution.

A failed HTTP request, terminal execution error or stream ending without a result
shows a recoverable error. Observed steps stay visible and unfinished steps are
marked interrupted. The unconfirmed user message stays in the local transcript,
and its text is restored in the composer. Reloading history reconciles a turn that
may have persisted just before disconnection and keeps unmatched pending messages.
It never automatically resends a POST: inspect history before explicitly sending
again. Unconfirmed messages are in-memory UI state; switching conversations or
reloading the entire page discards them. Completed messages remain in SQLite.

For a completely offline browser demo and repeatable checks, see
[UI verification](testing.md#web-ui-checks).

## Horizontal workflow map

The **Turn workflow** map spans both columns above the chat and execution panel.
It keeps fixed node positions and stays visible as the page scrolls on sufficiently
tall viewports. Collapse it with **Hide map**; events continue updating while it
is closed. On short viewports it scrolls with the page to leave room for the chat.
On small screens the graph scrolls horizontally, preserving readable node labels,
while the chat and execution panels stack vertically.

Green nodes have completed, amber nodes are running, gray nodes have not been
observed, purple highlights recovery and red marks failed/interrupted visits.
Each visited node shows a count such as `×2`. Status symbols, accessible labels,
per-visit details and the **Observed order** trail complement the colors. The
active-node pulse respects the reduced-motion preference. Repeated executions
reuse the same button without moving it or stealing keyboard focus.

The main map includes `check_cards`, clarification, both card exits and both
recovery paths: `review → retrieve_more → repair → validate`, and local
`review → repair → validate`. **Card subgraph** expands the actual
prepare/model/tools/collect/finish graph, including its model/tool loop and both
ways of reaching finish. The enclosing cards step can run at the same time as
its children; it is not a transition into each child. Only consecutive observed
starts within the same graph highlight an edge. Skipped or missing steps are not
filled in. Every completed route eventually reaches `remember`, which appends the
user/assistant messages to persistent history. The direct `check_cards → remember`
edge is for card-only searches or interactions blocked by unresolved/ambiguous
card evidence. A verified rules interaction takes `check_cards → decompose` and
reaches `remember` later through `finalize`, after generation/review/recovery.
An in-progress `repair` therefore does not yet show a completed `remember`. The terminal **End** lights only when the final `answer` event arrives;
it does not mean the response is factually approved.

Select a map node or trail visit to filter the existing execution list by that
node. Every occurrence retains its own duration, tool events and reported token
counts. An unvisited node explicitly says it has not run. Use **Show all steps**
to clear the filter. The trail records start order, including overlapping parent
and child occurrences; its arrows are chronology, whereas map arrows are graph
transitions. No model-generated explanation or additional API call is needed.

The map resets on a new turn, conversation switch or history reload. Completed
turns retain their map until then. Historical HTTP messages still contain prose
only: previous execution events are not persisted or reconstructed. No change
was made to the HTTP contract, graph policy or conversation storage.

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
cause abstention, not fabricated citations. Use `--conversation-id` in the CLI or
reuse an HTTP conversation ID for persistent follow-up turns.

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

## HTTP chat and SSE

Start the local API (one worker):

```sh
uv run --locked uvicorn backend.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal, create a conversation and copy the returned UUID:

```sh
curl -sS -X POST http://127.0.0.1:8000/conversations
CONVERSATION_ID='<returned UUID>'
curl -N -H 'Content-Type: application/json' \
  -d '{"message":"¿Qué es dañar primero?"}' \
  "http://127.0.0.1:8000/conversations/$CONVERSATION_ID/messages"
curl -sS "http://127.0.0.1:8000/conversations/$CONVERSATION_ID/messages"
```

While the streaming POST is active, send another POST to the same URL from a
third terminal: it returns 409 `conversation_busy`. Different conversations can
run concurrently. Use the same ID for follow-up questions and after a restart.
Real questions use paid model calls and may require the existing rules index.
Do not add curl retry flags: a lost connection can happen after a turn is stored.
See [HTTP API](http-api.md) for schemas, events, errors and cancellation limits.

Basic inspection commands (no model calls):

```sh
curl -sS http://127.0.0.1:8000/
curl -sS http://127.0.0.1:8000/health
curl -sS http://127.0.0.1:8000/version
curl -sS http://127.0.0.1:8000/conversations
```

Open `http://127.0.0.1:8000/docs` in a browser for interactive API help.
