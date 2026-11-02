"""Correlation of one turn with a simulated tracer, and answers when tracing is off.

The Langfuse SDK and its UI are not exercised. A fake client records the summary
and the callbacks the graph actually receives.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage

from backend.cards.mtg_api import MtgApiClient
from backend.common.prompts import PROMPT_VERSIONS
from backend.common.tracing import _ExportStatus, _LangfuseSession, summarize_turn, trace_turn
from backend.generation.answers import AnswerDraft, Claim
from backend.generation.llm import LLMConfig
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.review_policy import LEGACY_REVIEW_POLICY
from backend.workflow.state import Decomposition
from tests.test_workflow import card_reply, decision, review, source


class _RecordingHandler(BaseCallbackHandler):
    """Remember node namespace and tool call id. It does not send them anywhere."""

    def __init__(self, events):
        self.events = events

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        metadata = metadata or {}
        self.events.append({"event": "chain", "name": metadata.get("langgraph_node"),
                            "namespace": metadata.get("langgraph_checkpoint_ns")})

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        metadata = metadata or {}
        self.events.append({"event": "tool", "call_id": kwargs.get("tool_call_id"),
                            "namespace": metadata.get("langgraph_checkpoint_ns")})


class _SimulatedRetrieval:
    def __init__(self, session):
        self.session = session

    def end(self, payload):
        self.session.retrievals.append(payload)


class SimulatedSession:
    """In-memory stand-in for one Langfuse trace."""

    def __init__(self, session_id, name, metadata, *, fail_finish=False):
        self.session_id = session_id
        self.name = name
        self.metadata = {"langfuse_session_id": session_id, "langfuse_trace_name": name,
                         **{key: value for key, value in metadata.items() if isinstance(value, str)}}
        self.events = []
        self.callbacks = [_RecordingHandler(self.events)]
        self.retrievals = []
        self.finished = None
        self.trace_id = "simulated-trace"
        self.fail_finish = fail_finish

    def begin_retrieval(self, query, *, kind, edition):
        return _SimulatedRetrieval(self)

    def finish(self, payload):
        if self.fail_finish:
            raise RuntimeError("finish failed")
        self.finished = payload


class SimulatedTraceClient:
    """Records every opened turn. flush can be forced to fail."""

    def __init__(self, *, fail_finish=False, fail_flush=False):
        self.sessions = []
        self.flushed = 0
        self.fail_finish = fail_finish
        self.fail_flush = fail_flush

    def open(self, *, session_id, name, metadata):
        session = SimulatedSession(session_id, name, metadata, fail_finish=self.fail_finish)
        self.sessions.append(session)
        return session

    def flush(self):
        if self.fail_flush:
            raise RuntimeError("flush failed")
        self.flushed += 1


class TracingTests(unittest.TestCase):
    def workflow(self, llm, client=None, **settings):
        llm.config = LLMConfig()
        http = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"cards": [{"name": "Sample", "text": "First strike", "id": "p1"}]})))
        self.addCleanup(http.close)
        api = MtgApiClient(client=http, max_pages=1, retries=0)
        self.retriever = Mock()
        self.retriever.search.return_value = [source("100.1")]
        self.retriever.get.return_value = []
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings.setdefault("review_policy", LEGACY_REVIEW_POLICY)
        return QuestionWorkflow(self.retriever, llm=llm, api_factory=lambda: api, trace_client=client,
                                memory_path=Path(temporary.name) / "history.sqlite", **settings)

    def test_disabled_and_misconfigured_tracing_still_answers(self):
        self.assertNotIn("langfuse", sys.modules)
        llm = Mock()
        llm.generate.return_value = decision("clarify")
        disabled = {"LANGFUSE_TRACING_ENABLED": "false", "LANGFUSE_PUBLIC_KEY": "pk-test",
                    "LANGFUSE_SECRET_KEY": "sk-test"}
        with patch.dict("os.environ", disabled, clear=False):
            workflow = self.workflow(llm)
            state = workflow.answer("What is first strike?")
        self.assertEqual(state["reason"], "clarification")
        self.assertEqual(workflow.trace_report.status, "disabled")
        self.assertNotIn("langfuse", sys.modules)

        llm.generate.return_value = decision("clarify")
        missing = {"LANGFUSE_TRACING_ENABLED": "true", "LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}
        with patch.dict("os.environ", missing, clear=False):
            workflow = self.workflow(llm)
            state = workflow.answer("What is first strike?")
        self.assertEqual(state["reason"], "clarification")
        self.assertEqual(workflow.trace_report.status, "missing")
        self.assertEqual(workflow.trace_report.detail, "missing Langfuse keys")
        self.assertNotIn("langfuse", sys.modules)

    def test_simulated_client_correlates_session_and_turn(self):
        llm = Mock()
        llm.generate.side_effect = [
            decision(names=["Sample"]), card_reply(), Decomposition(queries=["first strike"]),
            AnswerDraft(can_answer=True, claims=[Claim(text="Grounded claim.", source_ids=["100.1"])], assumptions=[]),
            review(), decision("clarify")]
        llm.call_tools.side_effect = [
            AIMessage(content="", tool_calls=[{"name": "get_card_by_name", "args": {"name": "Sample"}, "id": "call-1"}]),
            AIMessage(content="Ready")]
        client = SimulatedTraceClient()
        workflow = self.workflow(llm, client, version="20260417")
        conversation_id = workflow.new_conversation()
        state = workflow.answer("Sample interaction", conversation_id=conversation_id)
        followup = workflow.answer("What?", conversation_id=conversation_id)

        self.assertIn("[100.1]", state["answer"])
        self.assertEqual(followup["reason"], "clarification")
        self.assertEqual(len(client.sessions), 2)
        self.assertEqual({item.session_id for item in client.sessions}, {conversation_id})
        self.assertEqual(client.sessions[0].name, "question-turn")
        self.assertEqual(client.sessions[0].metadata["langfuse_session_id"], conversation_id)
        self.assertEqual(client.flushed, 2)
        self.assertEqual(workflow.trace_report.status, "recorded")
        self.assertEqual(workflow.trace_report.trace_id, "simulated-trace")

        summary = client.sessions[0].finished
        self.assertEqual(set(summary["prompts"]),
                         {"route", "card_tools", "resolve_cards", "card_reply", "decompose", "answer", "review"})
        self.assertEqual(summary["prompts"]["route"]["version"], PROMPT_VERSIONS["route"])
        self.assertEqual(len(summary["prompts"]["route"]["sha256"]), 64)
        self.assertNotIn("repair", summary["prompts"])
        self.assertEqual(summary["queries"], ["first strike"])
        self.assertEqual(summary["retrieved_ids"], ["100.1"])
        self.assertEqual(summary["rules_edition"], "20260417")
        self.assertEqual(summary["source_editions"], ["test"])
        self.assertEqual(summary["model"], "gpt-6-luna")
        self.assertEqual(summary["tools"][0]["call_id"], "call-1")
        self.assertIn("Sample", summary["tools"][0]["arguments"])
        self.assertTrue(summary["review"]["approved"])
        self.assertEqual(summary["additional_retrievals"], 0)
        self.assertEqual(summary["repairs"], 0)
        self.assertIsNone(summary["error"])
        for forbidden in ("usage", "usage_details", "cost", "cost_details", "messages", "tool_messages"):
            self.assertNotIn(forbidden, summary)
        self.assertEqual(set(client.sessions[1].finished["prompts"]), {"route"})
        self.assertEqual(client.sessions[1].retrievals, [])

        queries = {item["query"] for item in client.sessions[0].retrievals}
        kinds = {item["kind"] for item in client.sessions[0].retrievals}
        self.assertEqual(queries, {"first strike"})
        self.assertEqual(kinds, {"rule", "glossary"})
        for item in client.sessions[0].retrievals:
            self.assertIn("100.1", item["source_ids"])
            self.assertIsNone(item["error"])
            self.assertGreaterEqual(item["latency_ms"], 0)
            for forbidden in ("usage", "usage_details", "cost", "cost_details"):
                self.assertNotIn(forbidden, item)

        chains = [item for item in client.sessions[0].events if item["event"] == "chain" and item["name"]]
        by_name = {item["name"]: item["namespace"] for item in chains}
        self.assertIn("finish", by_name)
        self.assertIn("cards", by_name)
        self.assertTrue(by_name["cards"].startswith("cards"))
        self.assertNotIn("|", by_name["cards"])
        self.assertIn("|", by_name["finish"])
        self.assertIn(by_name["cards"], by_name["finish"])
        tools = [item for item in client.sessions[0].events if item["event"] == "tool"]
        self.assertEqual(tools[0]["call_id"], "call-1")
        self.assertIn("cards", tools[0]["namespace"])
        self.assertIn("|", tools[0]["namespace"])

    def test_telemetry_and_search_failures_stay_visible(self):
        llm = Mock()
        llm.generate.return_value = decision("clarify")
        finishing = SimulatedTraceClient(fail_finish=True)
        workflow = self.workflow(llm, finishing)
        state = workflow.answer("What is first strike?")
        self.assertEqual(state["reason"], "clarification")
        self.assertEqual(workflow.trace_report.status, "missing")
        self.assertEqual(workflow.trace_report.detail, "RuntimeError")
        self.assertIsNone(finishing.sessions[0].finished)

        llm.generate.return_value = decision("clarify")
        flushing = SimulatedTraceClient(fail_flush=True)
        workflow = self.workflow(llm, flushing)
        state = workflow.answer("What is first strike?")
        self.assertEqual(state["reason"], "clarification")
        self.assertEqual(workflow.trace_report.status, "missing")
        self.assertEqual(flushing.sessions[0].finished["error"], None)

        llm = Mock()
        llm.generate.side_effect = [decision(), Decomposition(queries=["first strike"])]
        client = SimulatedTraceClient()
        workflow = self.workflow(llm, client)
        self.retriever.search.side_effect = RuntimeError("index down")
        with self.assertRaises(RuntimeError):
            workflow.answer("First strike?")
        self.assertEqual(client.sessions[0].retrievals[0]["error"], "RuntimeError")
        self.assertNotIn("index down", str(client.sessions[0].retrievals))
        self.assertEqual(client.sessions[0].finished["error"], "RuntimeError")
        self.assertNotIn("index down", str(client.sessions[0].finished))
        self.assertNotIn("usage_details", client.sessions[0].retrievals[0])

    def test_summary_omits_provider_messages(self):
        payload = summarize_turn({"question": "Hi", "messages": ["secret"], "tool_messages": ["cipher"],
                                  "prompts": {"route": {"name": "route", "version": "v2", "sha256": "abc"}},
                                  "context": []}, rules_edition=None)
        self.assertNotIn("messages", payload)
        self.assertEqual(payload["prompts"]["route"]["sha256"], "abc")
        self.assertIsNone(payload["error"])

    def test_export_failure_without_exception_does_not_claim_recorded(self):
        """Cover our export adapter, including mixed batches and sticky failures."""
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = Mock()
        exports = _ExportStatus(exporter)
        first = SimpleNamespace(context=SimpleNamespace(trace_id=1))
        second = SimpleNamespace(context=SimpleNamespace(trace_id=2))
        exporter.export.return_value = SpanExportResult.FAILURE
        exports.export([first])
        exporter.export.return_value = SpanExportResult.SUCCESS
        exports.export([first, second])
        session = _LangfuseSession(Mock(), SimpleNamespace(last_trace_id=format(1, "032x")), {}, exports)
        client = SimpleNamespace(open=lambda **kw: session, flush=lambda: None)
        with trace_turn(session_id="conversation", name="turn", metadata={}, client=client) as turn:
            pass
        self.assertEqual(turn.report.status, "missing")
        self.assertEqual(turn.report.detail, "Langfuse export failed or unconfirmed")
        session._handler.last_trace_id = format(2, "032x")
        with trace_turn(session_id="another-conversation", name="turn", metadata={}, client=client) as turn:
            pass
        self.assertEqual(turn.report.status, "recorded")
        self.assertIsNone(exports.consume(format(2, "032x")))

        exporter.export.side_effect = RuntimeError("sensitive transport error")
        with self.assertRaises(RuntimeError):
            exports.export([first])
        self.assertFalse(exports.consume(format(1, "032x")))
        # A flush with no observed spans (e.g. sampling disabled) is not success.
        with trace_turn(session_id="conversation", name="turn", metadata={}, client=client) as turn:
            pass
        self.assertEqual(turn.report.status, "missing")
