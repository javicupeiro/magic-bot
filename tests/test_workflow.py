"""Check graph integration and reference expansion; tool edge cases live elsewhere."""

import unittest
from unittest.mock import Mock

import httpx
from langchain_core.messages import AIMessage

from backend.cards.mtg_api import MtgApiClient
from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Claim
from backend.generation.llm import LLMConfig
from backend.ingestion.documents import Reference
from backend.retrieval.retrieval import SearchResult
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.references import context_size, expand_references
from backend.workflow.state import Decomposition, RouteDecision


def source(number, references=None):
    return SearchResult(number, "rule", number, "Evidence", 1, 1, "test", references=references or [])


def decision(route="rules", names=None):
    return RouteDecision(language="en", english_query="First strike", route=route,
                         card_names=names or [], clarification="Please clarify.")


def card_reply():
    return CardReply(language="en", text="Result.", needs_clarification=False, card_names=[])


class WorkflowTests(unittest.TestCase):
    def workflow(self, llm, handler=None, **settings):
        llm.config = LLMConfig()
        def unexpected(_):
            self.fail("Unexpected HTTP")
        http = httpx.Client(transport=httpx.MockTransport(handler or unexpected))
        self.addCleanup(http.close)
        api = MtgApiClient(client=http, max_pages=1, retries=0)
        self.retriever = Mock()
        self.retriever.search.return_value = [source("100.1")]
        self.retriever.get.return_value = []
        return QuestionWorkflow(self.retriever, llm=llm, api_factory=lambda: api, **settings)

    def test_rules_and_clarification_routes(self):
        for route in ("rules", "clarify"):
            llm = Mock()
            llm.generate.side_effect = [decision(route), Decomposition(queries=["First strike"]),
                AnswerDraft(can_answer=False, claims=[], assumptions=[])]
            state = self.workflow(llm).answer("What is first strike?")
            self.assertEqual(state["trace"][-1]["node"], "validate" if route == "rules" else "clarify")
            llm.call_tools.assert_not_called()

    def test_card_subgraph_stream_exposes_actual_steps(self):
        llm = Mock()
        llm.generate.side_effect = [decision("cards"), card_reply()]
        llm.call_tools.side_effect = [AIMessage(content="", tool_calls=[{
            "name": "get_card_by_name", "args": {"name": "Sample"}, "id": "call-1"}]), AIMessage(content="Ready")]
        workflow = self.workflow(llm, lambda _: httpx.Response(200, json={"cards": [{"name": "Sample"}]}))
        updates = list(workflow.stream("Find Sample"))
        nested = [name for namespace, update in updates if namespace for name in update]
        self.assertEqual(nested, ["prepare", "model", "tools", "collect", "model", "finish"])
        self.retriever.search.assert_not_called()

    def test_resolved_interaction_continues_to_rules(self):
        llm = Mock()
        llm.generate.side_effect = [decision(names=["Sample"]), card_reply(),
            Decomposition(queries=["first strike"]), AnswerDraft(can_answer=True,
            claims=[Claim(text="Grounded claim.", source_ids=["100.1"])], assumptions=[])]
        llm.call_tools.side_effect = [AIMessage(content="", tool_calls=[{
            "name": "get_card_by_name", "args": {"name": "Sample"}, "id": "call-1"}]), AIMessage(content="Ready")]
        state = self.workflow(llm, lambda _: httpx.Response(200, json={"cards": [
            {"name": "Sample", "text": "First strike", "id": "p1"}]})).answer("Sample interaction")
        self.assertEqual(state["verified_cards"][0]["text"], "First strike")
        self.assertIn("[100.1]", state["answer"])
        self.assertEqual(state["prompts"]["answer"]["version"], "v2")
        self.assertIn("resolve_cards", state["prompts"])

    def test_missing_card_resolution_stops_before_retrieval(self):
        llm = Mock()
        llm.generate.side_effect = [decision(names=["Sample"]), card_reply()]
        llm.call_tools.return_value = AIMessage(content="I know it")
        state = self.workflow(llm).answer("Sample interaction")
        self.assertEqual(state["reason"], "unresolved_card")
        self.retriever.search.assert_not_called()

    def test_empty_budget_and_invalid_citations_abstain(self):
        for budget, reason in [(100, "empty_context"), (24000, "invalid_citations_or_answer")]:
            llm = Mock()
            llm.generate.side_effect = [decision(), Decomposition(queries=["first strike"]),
                AnswerDraft(can_answer=True, claims=[Claim(text="Unsupported.", source_ids=["missing"])], assumptions=[])]
            state = self.workflow(llm, context_budget=budget).answer("First strike?")
            self.assertEqual(state["reason"], reason)
            self.assertFalse(state["draft"].can_answer)

    def test_parsed_references_include_sections_and_stop_after_one_hop(self):
        root = source("root", [Reference("section", "510", "section-510", True),
                               Reference("rule", "100.2", "rule-100.2", True),
                               Reference("rule", "100.2", "rule-100.2", True),
                               Reference("rule", "999", "unresolved", False)])
        section = source("section-510", [Reference("rule", "999.1", "not-followed", True)])
        retriever = Mock()
        retriever.get.side_effect = lambda identifier, **_: {"section-510": [section], "rule-100.2": [source("rule-100.2")]}[identifier]
        result = expand_references([root, root], retriever, max_extra=1)
        self.assertEqual([item.id for item in result], ["root", "section-510"])
        retriever.get.assert_called_once_with("section-510", version="test")
        self.assertEqual(expand_references([root], retriever, budget=context_size([root])), [root])
