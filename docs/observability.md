# Observability

Tracing is optional. Without Langfuse keys the graph runs exactly as before and
nothing is sent. One stored conversation is one Langfuse session. Each question
is one trace inside that session. `card-chat` does not store a conversation, so
each invocation is its own session and its own trace.

## Turn on tracing

Set both keys. Leave `LANGFUSE_TRACING_ENABLED` unset, or set it to `true`.
`false` keeps tracing off even when the keys are present. The host defaults to
Langfuse Cloud; set `LANGFUSE_BASE_URL` for another region or a server you run
yourself. This project does not ship a Langfuse server or manage prompts there.
Versioned Markdown files remain the prompt source.

```text
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_TRACING_ENABLED=false
```

Do not put keys in the frontend, in git, or on the command line. `magic-chat`
and `card-chat` flush the pending batch before they exit. If telemetry fails,
the answer is still printed and stderr says `Trace unavailable`. The same
status is `workflow.trace_report` (`disabled`, `recorded`, or `missing`).
`detail` is a short label, not the provider payload.

## What a trace contains

LangGraph receives one native callback at `answer()` or `stream()`. That single
invocation is the whole turn. Nested model and tool calls inherit it. The local
`trace` list in the workflow state is a separate, compact debug log and is not
the Langfuse trace.

| Piece | What it is |
| --- | --- |
| Session | Conversation id. The same id groups later turns. |
| Trace | One question. The root span is the graph run. |
| Node span | `route`, `retrieve`, `cards`, and the other graph nodes. |
| Card children | `prepare`, `model`, `tools`, `collect`, `finish`, nested under `cards`. |
| Generation | One model call. `finish` is its own generation, not part of the tool-calling call. |
| Retrieval span | One `search` call. Present because the retriever is not a LangChain retriever. |
| Turn metadata | Compact summary attached to the root span. |

The card parent span's namespace looks like `cards:<task>`. A child looks like
`cards:<task>|finish:<task>`. Tool observations keep `tool_call_id`. That value
is the same id stored as `call_id` on the exchange.

Token counts live on generation observations, and only when the provider
reported them. Parent spans, including `cards` and the root, do not carry usage.
Do not add a parent span's tokens to its children's tokens. Latency is the
observation's own start and end. A retrieval span also stores `latency_ms`.
Cost is left unset when usage or a model price is missing. A zero written by
the Langfuse callback on a failed observation that has no token usage is not a
measured price; ignore it. This project does not write zero itself, and it does
not keep a second token total.

The turn summary lists only prompts that ran (`name`, `version`, `sha256`), the
requested rules edition, editions present on the kept sources, the model,
retrieval queries, ids still in context after expansion and the character
budget, tool name, arguments, call id and status, the evidence review, the
extra retrieval round, the repair count, and the exception type if the turn
raised. Search spans list the ids each query returned before that budget.

## What is sent to Langfuse

When tracing is on, the native callbacks send the graph's node inputs and
outputs. That includes the question, selected history, rendered prompts, model
outputs, tool arguments, catalogue results, and retrieved rule text. Model
messages can include the encrypted reasoning item the chat model stores so a
later tool step can continue. API keys are not written into the summary.
Exception text is reduced to the exception type in that summary; a native
callback can still attach the runtime's own error string to a failed observation.

That payload is for Langfuse. It is not the public event stream. `magic-chat
--events` prints only the conversation id, namespace, and completed node names.
Raw messages, encrypted provider state, and secrets do not belong in a future
browser client.

## Inspect one trace

1. Export the keys, set `LANGFUSE_TRACING_ENABLED=true`, and run a card question
   and a rules question with the same `--conversation-id`. Note the id printed
   as `Conversation:`.
2. In Langfuse, open Sessions and select that id. There is one trace per question.
3. Open the trace. The root span is the graph. Its metadata field `turn` is the
   summary: prompt versions, queries, kept source ids, tools, and review.
4. Open a span named `retrieval` to see which query and kind returned which
   source ids. Compare those ids with `turn.retrieved_ids`, which is what
   remained after expansion and the character budget.
5. Open the generation observations for tokens. The tool-calling generation sits
   under `model`. The structured card reply is the separate generation under
   `finish`. Do not sum the `cards` parent.
6. On a tool observation, read `tool_call_id` and match it to `call_id` in the
   summary. The namespace shows which card-subgraph step ran the tool.

A missing trace is reported on stderr and the answer is unchanged. Run the same
question again after fixing the keys or the host; the conversation id can stay
the same, and the new question is a new trace in that session.
