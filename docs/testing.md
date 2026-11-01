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
- Workflow tests check routing, actual subgraph event visibility, interaction handoff and bounded reference expansion. They do not repeat every HTTP/tool edge case.

Prefer one regression for a real risk over many implementation-shaped assertions.
No coverage percentage target, CSS snapshot suite or exact LLM wording tests.
`answer-rules` remains a baseline for experiments, not a second policy to maintain.

## Manual checks after model-facing changes

Compare English/Spanish definitions, one named-card interaction, a card search,
an ambiguous name and a question with insufficient evidence. Inspect sources and
active prompt versions. Live checks cost provider tokens and should be explicit.
The current refactor is verified offline; it does not establish live model quality.

Retrieval quality cases and saved results remain in `data/retrieval/`. Do not
replace the original baseline when comparing a new method. A missed relevant
rule is an evaluation observation, not automatically a failing unit test.

Compare both methods with `uv run --locked benchmark-retrieval --method both --output data/retrieval/local-comparison.json`. This uses real embedding requests and predefined reviewed IDs; it measures retrieval rather than generated-answer quality. See the [paired experiment](ingestion-and-retrieval.md#paired-experiment).


`benchmark-retrieval --sweep --repeats 3 --output data/retrieval/parameter-sweep-local.json`
compares final result counts and hybrid candidate budgets with fixed judgments.
The suite checks this matrix using temporary Chroma and fake embeddings, including
repeated measurements, independent limits and retained source evidence. Live sweep
latencies include fresh provider calls and exclude separately timed lexical setup.
