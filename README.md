# Magic bot

A local Magic: The Gathering assistant built incrementally for learning. Python
prepares a rules/glossary index, answers grounded questions in English or Spanish,
and queries the live card API through native LangGraph tools.

The current implementation includes PDF ingestion, retrieval, grounded answers,
card catalogue tools, vector/hybrid retrieval, a chat workflow with bounded
answer review, persistent local conversations, optional Langfuse tracing, and a
local FastAPI backend with POST SSE execution events.
The web UI remains planned. Omitting a conversation ID starts a new conversation.

## Start locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12. From the repository root:

```sh
uv sync --locked
# If .env does not exist, copy .env.example and supply your key.
# Do not overwrite an existing .env.
uv run --locked pdf-to-text
uv run --locked parse-rules
uv run --locked ingest-rules
uv run --locked magic-chat '¿Qué es dañar primero?'
```

Uncached embeddings and model calls use `OPENAI_API_KEY` and incur provider costs.
The card API is queried live; no card catalogue is downloaded. Defaults and model
settings are documented in [.env.example](.env.example).

## Try one layer at a time

```sh
uv run --locked search-rules 'first strike' --kind rule
uv run --locked search-rules --rule 702.49a
uv run --locked card-catalogue name 'Lightning Bolt'
uv run --locked card-chat --trace 'Find Lightning Bolt'
uv run --locked magic-chat 'Find Lightning Bolt' --events
uv run --locked magic-chat 'What is first strike?' --json
```

`--events` displays native graph steps as they complete, including the card
subgraph. Run without it to see the final answer and citations. Use `--help` for
individual command options.

## Understand the project

- [Architecture and LangGraph diagrams](docs/architecture.md): responsibilities, routing and tools.
- [Command guide](docs/usage.md): commands, limits and result inspection.
- [Ingestion and retrieval](docs/ingestion-and-retrieval.md): PDF, JSONL, Chroma and cosine search.
- [Catalogue behavior](docs/catalogue.md): HTTP filters, pagination and errors.
- [Prompt conventions](docs/prompts.md): shared loader, explicit versions and hashes.
- [Testing](docs/testing.md): focused tests versus manual quality evaluation.
- [Tracing](docs/observability.md): sessions, traces, and what is sent to Langfuse.
- [HTTP API](docs/http-api.md): conversations, SSE events, errors and disconnects.

## Verify locally

```sh
uv run --locked python -m unittest discover -s tests -v
```

Tests use temporary databases and simulated model/HTTP responses. They do not
call paid APIs. Quality of real answers must still be evaluated separately.
