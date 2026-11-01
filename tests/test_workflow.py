"""Check graph integration and reference expansion; tool edge cases live elsewhere."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

import httpx
from langchain_core.messages import AIMessage

from backend.cards.mtg_api import MtgApiClient
from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Claim
from backend.generation.llm import LLMConfig, StructuredOutputError
from backend.ingestion.documents import Reference
from backend.retrieval.retrieval import SearchResult
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.references import context_size, expand_references
from backend.workflow.review_policy import LEGACY_REVIEW_POLICY
from backend.workflow.state import AnswerReview, Decomposition, RouteDecision


def source(number, references=None):
    return SearchResult(number, "rule", number, "Evidence", 1, 1, "test", references=references or [])


def decision(route="rules", names=None):
    return RouteDecision(language="en", english_query="First strike", route=route,
                         card_names=names or [], clarification="Please clarify.")


def card_reply():
    return CardReply(language="en", text="Result.", needs_clarification=False, card_names=[])


def review(supported=True, queries=None):
    """Provide factual objections for a rejected fixture, without model reasoning."""
    return AnswerReview(supported=supported,
                        unsupported_claims=[] if supported else ["The extra damage claim lacks support in 100.1."],
                        missing_information=[], followup_queries=queries or [])


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
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings.setdefault("review_policy", LEGACY_REVIEW_POLICY)
        return QuestionWorkflow(self.retriever, llm=llm, api_factory=lambda: api,
                                memory_path=Path(temporary.name) / "history.sqlite", **settings)

    def test_rules_and_clarification_routes(self):
        for route in ("rules", "clarify"):
            llm = Mock()
            llm.generate.side_effect = [decision(route), Decomposition(queries=["First strike"]),
                AnswerDraft(can_answer=False, claims=[], assumptions=[]), review(False)]
            state = self.workflow(llm).answer("What is first strike?")
            self.assertEqual(state["trace"][-1]["node"], "finalize" if route == "rules" else "clarify")
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
            claims=[Claim(text="Grounded claim.", source_ids=["100.1"])], assumptions=[]), review()]
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
                AnswerDraft(can_answer=True, claims=[Claim(text="Unsupported.", source_ids=["missing"])], assumptions=[]), review()]
            if budget == 100:
                # Empty context skips generation, but still gets an evidence review.
                llm.generate.side_effect = [decision(), Decomposition(queries=["first strike"]), review(False)]
            state = self.workflow(llm, context_budget=budget).answer("First strike?")
            self.assertEqual(state["reason"], reason)
            self.assertFalse(state["draft"].can_answer)

    def test_review_approval_repair_and_persistent_rejection(self):
        """Exercise real graph edges with simulated verdicts and one recovery cap."""
        grounded = Claim(text="A grounded part.", source_ids=["100.1"])
        unsupported = Claim(text="Invented extra damage.", source_ids=["100.1"])
        for scenario in ("approval", "repair", "persistent"):
            with self.subTest(scenario=scenario):
                initial = AnswerDraft(can_answer=True,
                    claims=[grounded] if scenario == "approval" else [grounded, unsupported], assumptions=[])
                repaired = AnswerDraft(can_answer=True, claims=[grounded,
                    Claim(text="Corrected damage.", source_ids=["100.2"])], assumptions=[])
                llm = Mock()
                outputs = [decision(), Decomposition(queries=["First strike"]), initial]
                if scenario == "approval":
                    outputs += [review()]
                else:
                    outputs += [review(False, ["damage", " damage ", " "]), repaired,
                                review(scenario == "repair", ["another query"])]
                llm.generate.side_effect = outputs
                workflow = self.workflow(llm, version="test", context_budget=1500)
                self.retriever.search.side_effect = [[source("100.1")], [], [source("100.2")], []]
                state = workflow.answer("How does damage work?")
                attempts = 0 if scenario == "approval" else 1
                self.assertEqual(state["additional_retrievals"], attempts)
                self.assertEqual(self.retriever.search.call_count, 2 + 2 * attempts)
                self.assertEqual(len(state["reviews"]), 1 + attempts)
                self.assertLessEqual(context_size(state["context"]), 1500)
                self.assertEqual(llm.generate.call_count, 4 + 2 * attempts)
                self.assertEqual(state["draft"].can_answer, scenario != "persistent")
                self.assertEqual(state["trace"][-1]["node"], "finalize")
                self.assertNotIn(unsupported.text, state["answer"])
                if scenario == "repair":
                    self.assertIn(grounded.text, state["answer"])
                    repair_prompt = llm.generate.call_args_list[4].args[0]
                    self.assertIn("100.2", repair_prompt)
                    self.assertIn(unsupported.text, repair_prompt)
                    self.assertEqual(state["prompts"]["repair"]["version"], "v1")
                if scenario == "persistent":
                    self.assertEqual(state["reason"], "review_budget_exhausted")
                    self.assertEqual(state["citations"], [])

    def test_rejected_citations_reach_reviewer_and_cannot_be_overridden(self):
        llm = Mock()
        invalid = AnswerDraft(can_answer=True,
            claims=[Claim(text="Bad reference.", source_ids=["missing"])], assumptions=[])
        llm.generate.side_effect = [decision(), Decomposition(queries=["First strike"]), invalid, review()]
        state = self.workflow(llm).answer("First strike?")
        prompt = llm.generate.call_args_list[-1].args[0]
        self.assertIn("Citation is outside retrieved context: missing", prompt)
        self.assertIn(invalid.claims[0].text, prompt)
        self.assertFalse(state["reviews"][0]["approved"])
        self.assertFalse(state["draft"].can_answer)
        self.assertEqual(state["additional_retrievals"], 0)

    def test_unavailable_review_or_repair_fails_closed(self):
        for fail_at in ("review", "repair"):
            with self.subTest(fail_at=fail_at):
                draft = AnswerDraft(can_answer=True,
                    claims=[Claim(text="Candidate.", source_ids=["100.1"])], assumptions=[])
                outputs = [decision(), Decomposition(queries=["First strike"]), draft]
                if fail_at == "review":
                    outputs += [StructuredOutputError("refused")]
                else:
                    outputs += [review(False, ["damage"]), StructuredOutputError("refused"), review()]
                llm = Mock()
                llm.generate.side_effect = outputs
                state = self.workflow(llm).answer("First strike?")
                self.assertFalse(state["draft"].can_answer)
                self.assertEqual(state["citations"], [])
                self.assertEqual(state["reason"], "unavailable_review" if fail_at == "review"
                                 else "unavailable_structured_repair")

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
