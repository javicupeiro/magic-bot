# Testing and evaluation

The automated suite checks deterministic behavior with fake model responses,
`httpx.MockTransport` and temporary databases. It does not call paid APIs or
claim to measure the quality of generated Magic rulings.

```sh
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m unittest discover -s tests -p test_card_tools.py -v
uv run --locked python -m unittest discover -s tests -p test_workflow.py -v
```

## Responsibilities

- Parsing tests protect source boundaries, IDs and references.
- Ingestion/retrieval tests protect persistence, embedding compatibility, cosine scores, source metadata, BM25 corpus correspondence and stable RRF fusion.
- Answer tests protect prompt contracts and shared citation/abstention policy.
- Catalogue tests own HTTP pagination, filters, failures and localized names.
- Tool tests own native call/result messages, schema rejection, budgets and evidence checks.
- Workflow tests check routing, actual subgraph event visibility, interaction handoff, bounded reference expansion, evidence approval/repair/abstention
  and the single additional retrieval round. They do not repeat every HTTP/tool edge case.

Prefer one regression for a real risk over many implementation-shaped assertions.
No coverage percentage target, CSS snapshot suite or exact LLM wording tests.
`answer-rules` remains a baseline for experiments, not a second policy to maintain.

## Manual checks after model-facing changes

Compare English/Spanish definitions, one named-card interaction, a card search,
an ambiguous name and a question with insufficient evidence. Inspect sources and
active prompt versions. Live checks cost provider tokens and should be explicit.
The current refactor is verified offline; it does not establish live model quality.

## Langfuse tracing

Run deterministic checks with `LANGFUSE_TRACING_ENABLED=false` so local Langfuse
credentials do not send test fixtures to the real project. `test_tracing.py`
injects its own clients or overrides that setting for configuration checks.
It covers session/turn correlation, prompt provenance, retrieval and tool ids,
telemetry failures, and exporter failures returned without exceptions. The
export-result adapter is tested with fake spans and a fake transport; the suite
does not test the Langfuse SDK or UI. See [observability](observability.md) for
the recorded live verification and inspection procedure.

Retrieval quality cases and saved results remain in `data/retrieval/`. Do not
replace the original baseline when comparing a new method. A missed relevant
rule is an evaluation observation, not automatically a failing unit test.

Compare both methods with `uv run --locked benchmark-retrieval --method both --output data/retrieval/local-comparison.json`. This uses real embedding requests and predefined reviewed IDs; it measures retrieval rather than generated-answer quality. See the [paired experiment](ingestion-and-retrieval.md#paired-experiment).


`benchmark-retrieval --sweep --repeats 3 --output data/retrieval/parameter-sweep-local.json`
compares final result counts and hybrid candidate budgets with fixed judgments.
The suite checks this matrix using temporary Chroma and fake embeddings, including
repeated measurements, independent limits and retained source evidence. Live sweep
latencies include fresh provider calls and exclude separately timed lexical setup.

## Evidence review checks

`test_review_approval_repair_and_persistent_rejection` runs three simulated
verdict sequences through the actual graph. It checks preserved supported text,
new evidence reaching repair, rejected text disappearing, cleared final citations
and a maximum of one extra retrieval round, even when the final review requests
more searches. Other workflow checks cover deterministic citation rejections
reaching the judge, contradictory approvals and unavailable review/repair output.
These assertions check orchestration, not a real model's semantic accuracy.

For a live comparison, inspect the first `validate` update from
`QuestionWorkflow.stream()` and compare it with the final `finalize` update.
This holds the initial draft/evidence fixed; the additional model calls belong
to review and optional repair. Alternatively compare the CLI baseline and workflow,
noting that their normalization and retrieval paths differ.

## Conversation memory

```sh
uv run --locked python -m unittest discover -s tests -p test_conversation_memory.py -v
```

These checks use temporary SQLite databases, real compiled graphs, deterministic
models and mocked catalogue HTTP. They cover restart/isolated conversations,
typed checkpoint reconstruction, resetting tools/evidence/drafts/review budgets,
full storage with bounded model history, atomic parallel tool pairs and local
turn exclusion. Existing workflow tests also use temporary memory databases.
See [conversation memory](conversation-memory.md) for a live CLI demonstration.
