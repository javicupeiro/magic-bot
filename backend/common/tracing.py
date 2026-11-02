"""Optional Langfuse trace for one conversation turn.

QuestionWorkflow and CardAgent open a turn at the graph entry point and pass
the native LangChain callback through. LangGraph then nests every node, model
call and tool under that trace. This module does not wrap those functions again.

A stored conversation is one Langfuse session. Each question is one trace.
Model calls are generations: the card subgraph's tool-calling step and the
structured ``finish`` call are separate generations, and token usage stays on
those generations so a parent span is not added a second time. Retrieval is not
a LangChain retriever, so each search records one explicit span with the query
and the ids it returned.

Telemetry failures are recorded on TraceReport and never replace the answer.
Short scripts flush before the turn context exits, so a process that ends
immediately still sends the pending batch.
"""

import base64
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import os
import sys
import time
from threading import Lock

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler


@dataclass
class TraceReport:
    """Outcome of one turn. ``detail`` is a short safe label, never a secret or payload.

    status is ``disabled`` when tracing is off, ``recorded`` after successful
    export, and ``missing`` when tracing could not be completed or confirmed.
    """

    status: str
    trace_id: str | None = None
    detail: str | None = None


class TracingUnavailable(RuntimeError):
    """Tracing was requested and could not start. The answer is still produced."""


class _Sources:
    """Ids and error collected around one search. ``sources`` is called by the node."""

    def __init__(self):
        self.source_ids: list[str] = []
        self.error: str | None = None

    def sources(self, identifiers):
        """Remember the ids this search returned, before any later budget drops them."""
        self.source_ids = [identifier for identifier in identifiers if isinstance(identifier, str)]


class _InactiveTurn:
    """No callbacks and no spans. Used when tracing is off or could not start."""

    active = False

    def __init__(self, report: TraceReport):
        self.report = report

    def apply(self, config):
        """Leave the graph config unchanged."""
        return config

    def fail(self, error):
        """Nothing was opened, so there is no trace to annotate."""

    def mark_missing(self, error):
        """Keep the first safe reason; later failures must not hide it."""
        if self.report.status != "missing":
            self.report.status = "missing"
            self.report.detail = _safe_detail(error)


class _ActiveTurn:
    """One open session. The outcome callback and the client share this object."""

    active = True

    def __init__(self, session, report: TraceReport, rules_edition):
        self.session = session
        self.report = report
        self.rules_edition = rules_edition
        self._finished = False
        # The outcome callback must run before the Langfuse handler ends the root
        # span, so the compact summary is attached while that span is still current.
        self.callbacks = [_OutcomeHandler(self), *session.callbacks]

    def apply(self, config):
        """Merge session metadata and callbacks. LangGraph adds the namespace later."""
        metadata = {**(config.get("metadata") or {}), **self.session.metadata}
        callbacks = [*(config.get("callbacks") or []), *self.callbacks]
        return {**config, "metadata": metadata, "callbacks": callbacks}

    def fail(self, error):
        """Record a graph failure without turning it into a successful turn."""
        if self._finished:
            return
        self.finish_state({}, error=error)

    def finish_state(self, state, *, error=None, replace=False):
        """Send the compact summary. Telemetry errors do not propagate."""
        if self._finished and not replace:
            return
        if error is not None and self._finished:
            return
        try:
            payload = summarize_turn(state, rules_edition=self.rules_edition, error=error)
            for key in ("model", "reasoning_effort"):
                if payload[key] is None:
                    payload[key] = self.session.metadata.get(key)
            self.session.finish(payload)
        except Exception as exc:
            self.mark_missing(exc)
            return
        self._finished = True
        if self.report.status != "missing":
            self.report.status = "recorded"
            self.report.trace_id = getattr(self.session, "trace_id", None)

    def mark_missing(self, error):
        """Remember that this trace is incomplete. Do not overwrite the first cause."""
        if self.report.status != "missing":
            self.report.status = "missing"
            self.report.detail = _safe_detail(error)


class _OutcomeHandler(BaseCallbackHandler):
    """Attach the turn summary to the root span. It does not create observations.

    Child runs keep the namespace LangGraph put in their metadata. Tool runs
    keep ``tool_call_id`` through the native tool callback. This handler only
    watches the root, so it does not add a second span per node.
    """

    def __init__(self, turn: _ActiveTurn):
        self.turn = turn

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        """Keep the last root output. Earlier empty roots must not win."""
        if parent_run_id is not None or not isinstance(outputs, dict) or "question" not in outputs:
            return
        self.turn.finish_state(outputs, replace=True)

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        """Preserve the failure type. The original exception still propagates."""
        if parent_run_id is None:
            self.turn.finish_state({}, error=error)


_current: ContextVar[_ActiveTurn | _InactiveTurn | None] = ContextVar("trace_turn", default=None)


def _safe_detail(error: BaseException) -> str:
    """Return a label that cannot contain keys, prompts or provider payloads."""
    if isinstance(error, TracingUnavailable):
        return str(error)
    return type(error).__name__


def _prompt_metadata(state):
    """Return only prompt versions that this turn actually loaded."""
    combined = dict(state.get("card_prompts") or {})
    combined.update(state.get("prompts") or {})
    prompts = {}
    for name, value in combined.items():
        if isinstance(value, dict) and value.get("version") and value.get("sha256"):
            prompts[name] = {"name": value.get("name", name), "version": value["version"], "sha256": value["sha256"]}
    return prompts


def _retrieved(state):
    """Ids and editions kept in context after expansion and the character budget."""
    identifiers = []
    editions = []
    for source in state.get("context") or []:
        identifier = getattr(source, "id", None)
        edition = getattr(source, "version", None)
        if isinstance(source, dict):
            identifier = identifier or source.get("id")
            edition = edition or source.get("version")
        if isinstance(identifier, str):
            identifiers.append(identifier)
        if isinstance(edition, str) and edition not in editions:
            editions.append(edition)
    return identifiers, editions


def _tools(state):
    """Tool name, arguments and call id. Catalogue bodies stay on the native tool span."""
    recorded = []
    for item in state.get("exchanges") or []:
        if not isinstance(item, dict):
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        error = result.get("error")
        recorded.append({
            "call_id": item.get("call_id"),
            "name": item.get("name"),
            "arguments": item.get("arguments"),
            "status": result.get("status"),
            "error": error.get("kind") if isinstance(error, dict) else None,
        })
    return recorded


def summarize_turn(state, *, rules_edition, error=None) -> dict:
    """Build the compact trace summary. It is not a token total and not the public UI event.

    Native callbacks already carry node inputs and outputs. This dictionary adds
    the fields that are hard to see there: executed prompt versions, the rules
    edition, the ids kept after expansion, tool call ids, and the review. It
    omits messages, encrypted provider state and any cost, including zero.
    """
    if not isinstance(state, dict):
        state = {}
    retrieved_ids, editions = _retrieved(state)
    reviews = state.get("reviews") or []
    review = reviews[-1] if reviews and isinstance(reviews[-1], dict) else None
    return {
        "question": state.get("question") if isinstance(state.get("question"), str) else None,
        "model": state.get("model"),
        "reasoning_effort": state.get("reasoning_effort"),
        "rules_edition": rules_edition,
        "source_editions": editions,
        "prompts": _prompt_metadata(state),
        "queries": list(state.get("queries") or []),
        "repair_queries": list(state.get("repair_queries") or []),
        "retrieved_ids": retrieved_ids,
        "tools": _tools(state),
        "review": review,
        "additional_retrievals": state.get("additional_retrievals") or 0,
        "repairs": state.get("repairs") or 0,
        "reason": state.get("reason"),
        "validation_error": state.get("validation_error"),
        "error": type(error).__name__ if error else None,
    }


@contextmanager
def retrieval_observation(query, *, kind, edition):
    """Record one search. No-op when this turn is not being traced.

    The span has latency and source ids, never token usage or a cost. A search
    exception is recorded and raised again; a telemetry exception is not.
    """
    turn = _current.get()
    holder = _Sources()
    started = time.perf_counter()
    span = None
    if turn is not None and turn.active:
        try:
            span = turn.session.begin_retrieval(query, kind=kind, edition=edition)
        except Exception as exc:
            turn.mark_missing(exc)
    try:
        yield holder
    except Exception as exc:
        holder.error = type(exc).__name__
        raise
    finally:
        if span is not None:
            payload = {
                "query": query,
                "kind": kind,
                "rules_edition": edition,
                "source_ids": list(holder.source_ids),
                "error": holder.error,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            try:
                span.end(payload)
            except Exception as exc:
                turn.mark_missing(exc)


def _false_flag(value: str) -> bool:
    return value.strip().lower() in {"0", "false", "no", "off"}


def _true_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def client_from_env():
    """Return a Langfuse client, or None when tracing is intentionally off.

    Both keys are required. ``LANGFUSE_TRACING_ENABLED=false`` forces tracing
    off even if keys are present. Asking for tracing without keys raises
    TracingUnavailable; the caller turns that into a missing trace.
    """
    load_dotenv()
    flag = os.getenv("LANGFUSE_TRACING_ENABLED", "")
    if _false_flag(flag):
        return None
    public = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public or not secret:
        if _true_flag(flag):
            raise TracingUnavailable("missing Langfuse keys")
        return None
    return _client(public, secret, os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))


@contextmanager
def trace_turn(*, session_id: str, name: str, metadata: dict, client=None):
    """Open one trace for this turn, yield it, and flush before returning.

    ``client`` is injected by tests. Otherwise the environment decides.
    Any failure while opening, annotating or flushing becomes ``missing``
    and is not raised.
    """
    turn = _InactiveTurn(TraceReport("disabled"))
    opened = False
    if client is None:
        try:
            client = client_from_env()
        except Exception as exc:
            client = None
            turn = _InactiveTurn(TraceReport("missing", detail=_safe_detail(exc)))
    if client is not None and turn.report.status != "missing":
        try:
            session = client.open(session_id=session_id, name=name, metadata=metadata)
            turn = _ActiveTurn(session, TraceReport("recorded"), metadata.get("rules_edition"))
            opened = True
        except Exception as exc:
            turn = _InactiveTurn(TraceReport("missing", detail=_safe_detail(exc)))
    token = _current.set(turn)
    try:
        yield turn
    finally:
        if opened:
            try:
                client.flush()
                verify = getattr(turn.session, "verify_export", None)
                if verify is not None:
                    verify()
            except Exception as exc:
                turn.mark_missing(exc)
        _current.reset(token)


def warn_missing_trace(report: TraceReport | None) -> None:
    """Tell the operator the answer was produced without a trace. Secrets are not printed."""
    if report is None or report.status != "missing":
        return
    detail = f" ({report.detail})" if report.detail else ""
    print(f"Trace unavailable{detail}. The answer was still produced.", file=sys.stderr)


class _LangfuseRetrieval:
    """One child span. Usage and cost are omitted so they cannot be summed twice."""

    def __init__(self, manager, observation):
        self._manager = manager
        self._observation = observation

    def end(self, payload):
        """Close the span with ids and latency. Unknown cost is left unset, not zero."""
        update = {
            "output": {"source_ids": payload["source_ids"], "error": payload["error"]},
            "metadata": {
                "query": payload["query"],
                "kind": payload["kind"],
                "rules_edition": payload["rules_edition"],
                "latency_ms": payload["latency_ms"],
            },
        }
        if payload["error"]:
            update["level"] = "ERROR"
            update["status_message"] = payload["error"]
        try:
            self._observation.update(**update)
        finally:
            self._manager.__exit__(None, None, None)


class _LangfuseSession:
    """Langfuse callback plus the explicit retrieval spans for one turn."""

    def __init__(self, langfuse, handler, metadata, exports):
        self._langfuse = langfuse
        self._handler = handler
        self.metadata = metadata
        self.callbacks = [handler]
        self._trace_id = None
        self._exports = exports

    @property
    def trace_id(self):
        """Trace id assigned when the root run started, if the handler has one."""
        return self._trace_id or getattr(self._handler, "last_trace_id", None)

    def begin_retrieval(self, query, *, kind, edition):
        """Nest a retrieval span under the current node. No usage and no cost."""
        manager = self._langfuse.start_as_current_observation(
            name="retrieval",
            as_type="span",
            input={"query": query, "kind": kind, "rules_edition": edition},
        )
        return _LangfuseRetrieval(manager, manager.__enter__())

    def finish(self, payload):
        """Attach the summary to the current root span without copying token usage."""
        update = {"metadata": {"turn": payload}}
        if payload.get("error"):
            update["level"] = "ERROR"
            update["status_message"] = payload["error"]
        self._langfuse.update_current_span(**update)
        self._trace_id = getattr(self._handler, "last_trace_id", None)

    def verify_export(self):
        """Check this trace's actual exporter result after flushing the SDK queue.

        OpenTelemetry reports HTTP/auth/network failures as a return value, not
        necessarily an exception from flush(). Missing evidence is also a failure:
        a disabled sampler or broken callback must not claim a recorded trace.
        """
        if self._exports.consume(self.trace_id) is not True:
            raise TracingUnavailable("Langfuse export failed or unconfirmed")


class _ExportStatus:
    """Observe export results without changing SDK spans, batching or retries.

    A batch can contain several concurrent turns, and a turn can span several
    batches. Failure is sticky per trace. Keep only 1024 recent outcomes so an
    interrupted caller cannot leave an unbounded registry behind.
    """

    def __init__(self, exporter):
        self.exporter = exporter
        self._outcomes = OrderedDict()
        self._lock = Lock()

    def export(self, spans):
        """Remember success/failure for each trace, including exporter exceptions."""
        from opentelemetry.sdk.trace.export import SpanExportResult

        successful = False
        try:
            result = self.exporter.export(spans)
            successful = result is SpanExportResult.SUCCESS
            return result
        finally:
            with self._lock:
                for span in spans:
                    trace_id = format(span.context.trace_id, "032x")
                    self._outcomes[trace_id] = self._outcomes.get(trace_id, True) and successful
                    self._outcomes.move_to_end(trace_id)
                while len(self._outcomes) > 1024:
                    self._outcomes.popitem(last=False)

    def consume(self, trace_id):
        """Remove a completed turn's result; None means export was not observed."""
        with self._lock:
            return self._outcomes.pop(trace_id, None)

    def shutdown(self):
        """Let the SDK close its normal HTTP exporter."""
        self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        """Delegate the optional exporter flush protocol."""
        return self.exporter.force_flush(timeout_millis)


class _LangfuseClient:
    """Create the native callback only after keys are known to be present."""

    def __init__(self, public, secret, base_url):
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        # Use the SDK's documented custom-exporter hook. Authentication and a
        # bounded timeout must be explicit when supplying our own exporter.
        authorization = base64.b64encode(f"{public}:{secret}".encode()).decode("ascii")
        self._exports = _ExportStatus(OTLPSpanExporter(
            endpoint=f"{base_url.rstrip('/')}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {authorization}", "x-langfuse-ingestion-version": "4"},
            timeout=5,
        ))
        self._langfuse = Langfuse(public_key=public, secret_key=secret, base_url=base_url,
                                  span_exporter=self._exports)
        self._handler_type = CallbackHandler
        self._public = public

    def open(self, *, session_id, name, metadata):
        """One handler per turn so sessions are not mixed across conversations."""
        flat = {key: value for key, value in metadata.items() if isinstance(value, str) and value}
        return _LangfuseSession(self._langfuse, self._handler_type(public_key=self._public), {
            "langfuse_session_id": session_id,
            "langfuse_trace_name": name,
            **flat,
        }, self._exports)

    def flush(self):
        """Send the batch before a short script exits."""
        self._langfuse.flush()


@lru_cache(maxsize=1)
def _client(public, secret, base_url):
    """Reuse the client/exporter pair across turns, as Langfuse shares resources."""
    return _LangfuseClient(public, secret, base_url)
