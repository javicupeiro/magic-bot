# Prompt conventions

All conversational instructions live in `backend/prompts/<name>/<version>.md`.
The reusable loader is `backend/common/prompts.py`; it has no dependency on the
LLM client, retrieval or LangGraph.

## Loading

```python
from backend.common.prompts import load_prompt

prompt = load_prompt("normalize")
text = prompt.render(question_json='"What is mana?"')
metadata = prompt.metadata()
```

`PROMPT_VERSIONS` explicitly selects the active version for every prompt. Passing
`version="v1"` selects an older version for a deliberate comparison. There is no
implicit latest-file lookup. The caller loads a template when the step needs it.

Each file declares `Name`, `Version` and `Variables`, then its English instructions.
`render()` requires exactly those variables and substitutes them once. The loader
checks the file/header version and records the SHA-256 of the original bytes.

## Active versions

- `normalize/v1`: language and English query for the simple RAG baseline.
- `route/v2`: resolve follow-ups from bounded history, then return route, language, self-contained English intent and named cards. `route/v1` remains available for comparison.
- `decompose/v1`: one to three retrieval queries.
- `answer/v2`: grounded claims from rules and optionally verified cards.
- `review/v1`: evidence verdict, factual objections and at most three follow-up queries.
- `repair/v1`: correction using bounded updated evidence and the recorded objections.
- `card_tools/v2`: tool selection and catalogue limitations.
- `resolve_cards/v1`: resolve supplied names before reasoning about an interaction.
- `card_reply/v1`: final structured card reply from collected evidence.

The committed `answer/v1` is preserved. Its later edits were moved into `v2`.
`card_tools/v1` is retained as the previous manual-cycle prompt, but is inactive.

## Making a change

1. Copy the existing prompt to a new version file and update its header.
2. Change the instructions and review the declared variables.
3. Select the version in `PROMPT_VERSIONS`.
4. Run the loader tests and manually compare relevant evaluation examples.
5. Record the version/hash with results. Do not overwrite a version already used.

Do not concatenate extra instructions in node functions. New roles or policies
belong in another versioned Markdown template. Tool function docstrings remain
beside the `@tool` declaration because they describe that callable's API; ordinary
error/clarification UI strings are also not model prompts.

A hash detects an edit; it does not enforce immutable history. Version discipline
and code review are still required. User questions and retrieved text are data,
not sources of system instructions.
