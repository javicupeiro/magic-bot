# Earlier observability validation

Historical measurements of tracing. The current configuration and
inspection procedure are in [observability](../reference/observability.md); delivery status
is in [evaluation results](../evaluation.md). These observations do not
establish current remote endpoint availability.

## Stage verification

The tracing-stage check ran **78 versioned tests**, all passing,
with tracing disabled for local fixtures. This is a historical stage result; the
[README](../../README.md#tests-and-evaluation) provides the current full-suite command.
The five tracing tests cover correlation, metadata, prompts, searches/tools,
disabled/incomplete configuration, end/flush failures without lost answers,
propagated retrieval errors, safe summary fields, failure-without-exception,
sticky failure across batches, independent traces and unconfirmed export.

Services are simulated. A separate real-callback check with failed simulated
transport observed an export attempt and retained `missing`. Conversation restart
also verifies reconstruction of `EvidenceReview` from SQLite. Its serializer type
was explicitly allowlisted after a validation warning, without enabling pickle
or arbitrary imports.

### Recorded live validation

Three real `magic-chat` turns used temporary SQLite under session
`tracing-validation-20261102`:

| Case | Trace |
| --- | --- |
| Lightning Bolt lookup | [Open trace](https://cloud.langfuse.com/project/cmvz5iaik01luad0jtbdafvam/traces/9b5a2ccdc9b02a35382588a40514d07d) |
| Lightning Bolt and protection from red | [Open trace](https://cloud.langfuse.com/project/cmvz5iaik01luad0jtbdafvam/traces/728d90a95183f5d90b1c6c455ee63a89) |
| White Warriors with filters | [Open trace](https://cloud.langfuse.com/project/cmvz5iaik01luad0jtbdafvam/traces/0df6fd30f182ca3b7940439fc172619a) |

API inspection confirmed three roots and 17 generations, with direct usage only
on generations. Both tools, call IDs, namespaces and separate `model`/`finish`
generations were verified. The interaction's two retrieval spans preserved queries
and IDs. The root summary retained 28 sources, prompt versions/hashes, one repair
and an approved final review. A synthetic `stream()` turn also exported.

This verifies instrumentation and data flow, not general ruling accuracy or
resilience to every network failure. The later Docker delivery trace's visual
inspection still requires authorized access; earlier API inspection does not
complete that separate check.

## Historical API inspection

The following observation query was used during the recorded validation; it reads
telemetry rather than running the bot:

```python
from dotenv import load_dotenv
from langfuse import get_client

load_dotenv()
langfuse = get_client()
response = langfuse.api.observations.get_many(
    session_id="tracing-validation-20261102",
    fields="core,basic,metadata,usage",
    expand_metadata="turn.retrieved_ids,turn.queries,turn.tools",
    limit=100,
)
```

Reconstruct parentage using `trace_id`, `id` and `parent_observation_id`; follow
the pagination cursor if more results exist. `expand_metadata` requests complete
values; the observed default truncates long metadata at 200 characters, which does
not establish loss of stored data. The observed endpoint rejected
`parse_io_as_json=True` and returned input/output as strings. The final delivery's
older REST read endpoint returned 410; export confirmation and UI access remain
separate checks, as recorded in the [delivery evaluation](../evaluation.md).
