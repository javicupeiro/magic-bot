# Local HTTP API and execution events

Run from the repository root:

```sh
uv sync --locked
uv run --locked uvicorn backend.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

FastAPI supplies request validation, OpenAPI and streaming responses. Uvicorn is
its ASGI server. These are the only new dependencies; there is no extra model or
external service. `/docs` exposes the route documentation. Real questions retain
the CLI's provider configuration, charges, rules index and catalogue dependencies.
The frontend does not receive credentials or select graph/provider settings.

## Basic endpoints

- `GET /` returns a welcome message and relative links to documentation, health,
  version, conversations and the [web UI](../usage.md#web-chat-and-execution-panel) at `/ui/`.
- `GET /health` returns `{"status":"ok"}` when the HTTP application responds.
  It does not probe SQLite, the rules index, the model or external services.
- `GET /version` returns `{"name":"magic-bot","version":"0.1.0"}` (using the
  installed package version). OpenAPI uses that same version.
- `GET /docs` provides interactive help; `GET /openapi.json` provides the contract.

## Conversation contract

- `GET /conversations` returns HTTP 200 with an array of
  `{"conversation_id":"<uuid>"}` records in creation order, including empty
  conversations. An empty database returns `[]`. This basic local listing is
  unpaginated and survives restart; it does not execute the graph.
- `POST /conversations` (no body) returns HTTP 201 with
  `{"conversation_id":"<uuid>"}`. Empty conversations are persisted immediately.
- `GET /conversations/{id}/messages` returns HTTP 200 with `conversation_id` and
  `messages`, an ordered list of `{ "role": "user" | "assistant", "content": "..." }`.
  Only completed turns are included; interrupted checkpoints are not chat history.
- `POST /conversations/{id}/messages` accepts exactly
  `{"message":"user question"}`. The string must contain non-whitespace text and
  have at most 16,000 characters. Extra keys are rejected. Original prose is kept.
  HTTP 200 is a `text/event-stream` response to this same POST.

IDs must be UUIDs created through this HTTP API. CLI-only conversation IDs are
not automatically registered. The small `api_conversations` table shares the
checkpoint SQLite file (`data/conversations/checkpoints.sqlite` by default).
History reads the existing checkpoint messages; it does not create a parallel
message store. Empty conversations and completed history survive server restart.

Before streaming, invalid bodies/IDs return 422 (`invalid_request`), missing
conversations return 404 (`conversation_not_found`), busy conversations return
409 (`conversation_busy`), and setup failures return 500 (`execution_failed`).
Errors use `{"detail":{"code":"..."}}`; validation also includes a short safe
message. Request bodies and provider exception text are never reflected in errors.
A failed POST is never automatically retried by the server.

## SSE contract

Each frame has `id: <turn_id>:<sequence>`, `event: <type>` and one JSON `data:` line,
followed by a blank line. Prose newlines are escaped inside JSON. Every payload
includes `type`, `conversation_id`, `turn_id` and `sequence` (starting at one).
`step` and opaque `step_id` identify the relevant occurrence when applicable;
`duration_ms` measures elapsed wall time for completed steps/tools and the turn.
Repeated nodes get separate step IDs. IDs are public and have no provider meaning.
Optional fields are omitted, not populated with invented zeroes.

Events are:

- `turn_started`: emitted from the native graph root callback after acquiring the
  conversation lock, before model work completes.
- `step_started`: actual node callback. Steps include `route`, `decompose`,
  `retrieve`, `expand`, `generate`, `validate`, `review`, `retrieve_more`, `repair`,
  `finalize`, `clarify`, `remember`, `cards` and `check_cards`. Native card steps
  are `cards.prepare`, `cards.model`, `cards.tools`, `cards.collect`, `cards.finish`.
- `step_completed`: matched native namespace/node update, with the same `step_id`
  as its start and `duration_ms`. Parent `cards` encloses its subgraph steps.
- `tool`: allowlisted catalogue `tool` name, `status` (`started`, `completed` or
  `failed`), associated step, and duration at completion. Arguments, raw result
  bodies and provider call IDs are intentionally absent.
- `model_usage`: observed integer `input_tokens`, `output_tokens`, `total_tokens`
  when present in native model usage metadata. Counts remain per model call;
  parents are not counted again. No event is invented when usage is unavailable.
- `sources`: final validated `sources`, emitted after successful graph completion.
- `answer`: terminal success. `result` contains `answer` (prose), `cards` and
  `sources`. Cards expose only `name`, `mana_cost`, `cmc`, `colors`, `color_identity`,
  `text`, `image_url`. Sources retain `source_id`, `kind`, `title`, `excerpt`,
  `page_start`, `page_end`, `version`. Clarification and abstention are successful
  answers too; sources/cards may be empty.
- `error`: a step callback can report `step_failed` with its step. Terminal
  `execution_failed` closes the failed turn without `answer`. Text is a safe
  technical label, never exception bodies or model reasoning.

For example, the final frame's JSON has this shape:

```json
{"type":"answer","conversation_id":"...","turn_id":"...","sequence":9,"duration_ms":42.0,"result":{"answer":"Please clarify.","cards":[],"sources":[]}}
```

Use streaming `fetch()` for POST, consume `response.body` incrementally, retain
incomplete frame text between reads, split on blank lines, and parse each `data:`
line as JSON. Check `response.ok` before parsing SSE. Browser `EventSource` uses
GET and does not fit this endpoint. A read chunk need not contain a whole frame.
There is no token streaming, heartbeat, reconnect replay or `Last-Event-ID` support.
A client must treat EOF without `answer` or terminal `error` as an uncertain result.

## Execution, cancellation and resources

`create_app()` injects a workflow factory for deterministic checks. Each POST
creates a fresh workflow and runs its synchronous iterator in one executor
thread, leaving the ASGI event loop available. The default index opens lazily
inside that thread only when retrieval needs it. Eight threads bound concurrent
execution; excess requests wait for capacity. This is a local demo, not a public
admission-control design.

`QuestionWorkflow.stream(callbacks=[adapter], blocking=False)` merges the public
observer with the existing optional Langfuse callbacks. It uses the same
`conversation_turn` lock as CLI turns. HTTP conflicts do not wait for the existing
turn. Native callbacks show starts/errors/tools/usage; updates from
`stream(subgraphs=True)` show actual completions. `EventAdapter` maps private
namespaces and run ancestry to public step IDs and copies only permitted fields.
The adapter retains the final root answer/cards/citations from that single stream.
It never calls `answer()` or invokes the graph again to reconstruct a result.
No LLM is called to explain the execution.

A bounded queue (256 frames) provides backpressure. On disconnect the ASGI
consumer signals a thread-safe stop event; callbacks/iteration stop cooperatively.
The iterator is closed in its owning worker, which releases its catalogue HTTP
client, SQLite connection and turn lock. Server shutdown also signals consumers
and waits for workers. Python cannot forcibly cancel an in-flight synchronous
provider/HTTP request: resources/lock can remain occupied until it returns or
its configured timeout expires. A queued task can be cancelled before it starts.

A turn may finish and persist just before a disconnect. Clients must fetch history
and let the user decide whether to send again. **Do not automatically retry POST**:
there is no idempotency key, so retrying can duplicate model charges and messages.
Completed user/assistant pairs remain available; a later explicit turn resets
transient evidence after interruption, following the existing workflow policy.

Only one process/worker is supported for local SQLite turn exclusion. Production
authentication, cross-process locking and public deployment are outside this API.
The demo serves `frontend/` at `/ui/` from the same origin without a permissive
CORS policy. Static files do not change the conversation/event schemas. Tool
arguments, subqueries, review verdicts, model names and trace URLs remain unavailable
to the UI; it explicitly labels these limits.

## Verification notes

Automated command:

```sh
LANGFUSE_TRACING_ENABLED=false uv run --locked python -m unittest discover -s tests -p test_http_api.py -v
```

Three HTTP integration tests passed using real compiled graphs, temporary SQLite,
`FakeMessagesListChatModel` usage callbacks and mocked catalogue HTTP. They verify
subgraph start/completion correlation, tool/model events, sequential frames, one
execution, safe output, persistent history after restart, contract errors, busy
conflicts, failed turns and recovery. A blocked fake model also proves history
and a competing POST remain responsive while the first turn runs.

The full suite command ran 88 tests: 87 passed; the existing
`test_probes_are_isolated_and_empty_context_restores_retrieval` failed because its
mock runs out of responses in `repair` (`StopIteration`). Running the evaluation
suite against `HEAD`'s original `QuestionWorkflow` reproduced the same failure;
the HTTP changes do not fix or hide that unrelated evaluation fixture.

A real Uvicorn server on an ephemeral loopback port was exercised with `curl`
using a temporary database and a blocked simulated model: conversation creation,
`curl -N` receiving `turn_started` before the model was released, a concurrent
409, final answer, history, and disconnect with `curl --max-time 0.3`. After the
simulated call returned, workflow cleanup completed and an explicit subsequent
POST succeeded. No paid model or external catalogue requests were made.
