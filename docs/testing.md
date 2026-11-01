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
- Ingestion/retrieval tests protect persistence, embedding compatibility, cosine scores and source metadata.
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
