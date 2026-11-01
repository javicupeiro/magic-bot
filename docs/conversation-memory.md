# Conversation memory

Storage and model context have different lifetimes. SQLite keeps completed user
and assistant messages for the whole conversation. A model request receives only
recent complete turns needed to interpret the current question. Card evidence,
rule sources, drafts, reviews and native tool execution are regenerated each turn.

## Commands

```sh
uv sync --locked
uv run --locked magic-chat 'Busca criaturas elfo verdes.' --conversation-id elves
# This is another process: it reopens the same checkpoint file.
uv run --locked magic-chat '¿y que cueste uno?' --conversation-id elves
# No previous filters: clarification is expected for an ambiguous follow-up.
uv run --locked magic-chat '¿y que cueste uno?' --conversation-id separate
```

Omit `--conversation-id` to allocate a fresh UUID automatically. Normal output
prints the ID; JSON includes `conversation_id`; event output begins with an ID
object, followed by native node updates. Use `--memory-db tmp/demo.sqlite` on all
commands to keep a demonstration separate from your regular history.

Card searches require configured model credentials and catalogue connectivity.
Rules also require the existing rules index. Opening the checkpoint database does
not open Chroma. `card-chat` and `answer-rules` keep their independent interfaces.

## Python lifecycle

```python
from backend.retrieval.retrieval import Retriever
from backend.workflow.graph import QuestionWorkflow

with QuestionWorkflow(Retriever()) as workflow:
    conversation_id = workflow.new_conversation()
    first = workflow.answer("Busca criaturas elfo verdes.", conversation_id=conversation_id)
    second = workflow.answer("¿y que cueste uno?", conversation_id=conversation_id)

# A new workflow can resume after restart using the same ID and memory_path.
```

The workflow owns SQLite and the context-managed catalogue client, compiles once
on entry, and releases both on exit, including exceptions. An injected external
HTTP pool remains caller-owned under `MtgApiClient`'s existing contract. A call
outside a `with` block opens and closes resources automatically. For a server,
open once, await all active calls/streams before closing, and use the same
conversation ID for each turn. Close partially consumed stream iterators.

The process-local conversation lock covers loading the checkpoint, selecting
history and the entire graph execution. Different IDs can progress independently.
Lock entries are discarded once no callers hold or await them. SQLite access is
managed by the native saver. There is no distributed coordination between backend
processes and no authentication layer.

## Reading the implementation

- `graph.py`: `__enter__` prepares live resources; `_prepare_turn` loads stored
  messages and replaces transient state; `answer`/`stream` hold the turn lock;
  `remember` appends the completed question and answer on every terminal route.
- `memory.py`: `open_memory` owns the SQLite connection and explicitly permits
  the application's typed records in the native serializer, without pickle;
  `conversation_turn` prevents read/modify/write races between local turns.
- `context.py`: `select_history` keeps a suffix of complete human-led turns.
  Native parallel tool calls and results stay together; incomplete groups are
  excluded. An oversized current message fails instead of being truncated.
- `nodes.py` and `route/v2`: the interpreter receives bounded recent messages
  and the original question, producing a self-contained intent. Generation and
  review use that resolved intent with newly retrieved evidence.
- `cards/agent.py`: the shared native subgraph receives original and resolved
  question data, starts with fresh messages and spends a fresh tool budget.
- `state.py`: distinguishes complete `messages`, selected `history`, original
  `question`, `resolved_question` and replaceable per-turn channels.
- `cli.py`: exposes conversation IDs, SQLite path and history limit.

## Budgets and limits

`history_budget` / `--history-budget` defaults to 8,000 serialized message
characters, including the current question. Old turns are removed from the model
view as whole groups. If the newest complete turn cannot fit, older turns are
not substituted, which avoids misleading gaps. Full `messages` remains in SQLite.
A long conversation can therefore be recovered even when `history` is empty.

The history cap is separate from the 24,000-character evidence budget: history
cannot consume the evidence allowance or grow to fill the provider window.
Instructions, structured schemas and output need additional capacity. These are
application character limits, not a tokenizer or an exact provider context-window
reservation; select conservative limits for the configured model. Existing card
payloads can exceed the evidence allowance, as documented in the review guide.
Only interpretation receives history; later stages use the compact resolved
intent, rather than repeatedly sending the entire conversation.

Old checkpoints also retain past state and evidence locally. Starting a new turn
clears current channels, not database history. Failed turns may leave intermediate
checkpoints, but only completed prose enters conversation history. No automatic
summaries, semantic user memory, retention/compaction or production identity
management are introduced.
