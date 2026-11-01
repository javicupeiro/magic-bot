"""Verify durable conversation isolation, context selection and local turn locks."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.cards.mtg_api import MtgApiClient
from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Citation, Claim
from backend.generation.llm import LLMConfig
from backend.ingestion.documents import Reference
from backend.retrieval.retrieval import SearchResult
from backend.workflow.context import message_size, select_history
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.memory import conversation_turn
from backend.workflow.state import AnswerReview, Decomposition, RouteDecision


class ConversationMemoryTests(unittest.TestCase):
    """Exercise real SQLite and graphs with deterministic models and HTTP."""

    def workflow(self, database):
        """Create independently owned dependencies, as a restarted backend would."""
        llm = Mock(config=LLMConfig())
        llm.generate.side_effect = self.generate
        llm.call_tools.side_effect = [
            AIMessage(content="", tool_calls=[{
                "id": "lookup", "name": "search_cards", "args": {"name": "Elf"}}]),
            AIMessage(content="Done"),
        ]
        http = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"cards": [{"name": "Elf"}]})))
        self.addCleanup(http.close)
        api = MtgApiClient(client=http, max_pages=1, retries=0)
        workflow = QuestionWorkflow(Mock(), llm=llm, api_factory=lambda: api, memory_path=database)
        return workflow, llm

    @staticmethod
    def generate(prompt, schema):
        """Simulate interpretation; assertions inspect actual prompts separately."""
        if schema is RouteDecision:
            return RouteDecision(language="es", english_query="Find green elves costing one",
                                 route="cards", card_names=[], clarification="")
        return CardReply(language="es", text="Elfos verdes encontrados.",
                         needs_clarification=False, card_names=[])

    def test_restart_isolation_and_turn_reset(self):
        """Resume native tool execution after restart without reusing old calls."""
        with TemporaryDirectory() as directory:
            database = Path(directory) / "history.sqlite"
            first, _ = self.workflow(database)
            result = first.answer("Busca elfos verdes", conversation_id="elves")
            self.assertIsNone(first.graph)
            self.assertEqual(len(result["messages"]), 2)
            self.assertEqual(len(result["exchanges"]), 1)

            restarted, llm = self.workflow(database)
            with restarted:
                graph = restarted.graph
                result = restarted.answer("¿y que cueste uno?", conversation_id="elves")
                prompt = llm.generate.call_args_list[0].args[0]
                self.assertIn("Busca elfos verdes", prompt)
                self.assertIn("¿y que cueste uno?", prompt)
                self.assertEqual(len(result["messages"]), 4)
                self.assertEqual(len(result["exchanges"]), 1)
                self.assertEqual(len(result["tool_messages"]), 5)
                self.assertEqual(result["additional_retrievals"], 0)
                self.assertEqual(result["context"], [])
                self.assertEqual(result["reviews"], [])
                self.assertIs(restarted.graph, graph)
                self.assertIn("Find green elves costing one", llm.call_tools.call_args_list[0].args[0][-1].content)

                llm.call_tools.side_effect = [AIMessage(content="Done")]
                isolated = restarted.answer("¿y que cueste uno?", conversation_id="new")
                self.assertEqual(isolated["history"], [])
                self.assertEqual(len(isolated["messages"]), 2)
                self.assertNotIn("Busca elfos verdes", llm.generate.call_args_list[2].args[0])
                self.assertEqual(isolated["exchanges"], [])
                self.assertEqual(len(isolated["tool_messages"]), 3)
            self.assertIsNone(restarted.graph)

    def test_context_keeps_current_and_complete_parallel_tool_pairs(self):
        """A budget drops old turns atomically while full storage remains intact."""
        old = [HumanMessage("old " * 100), AIMessage("old answer")]
        recent = [HumanMessage("Find elves"), AIMessage(content="", tool_calls=[
            {"id": identifier, "name": "search_cards", "args": {}}
            for identifier in ("a", "b")]),
            ToolMessage(content="one", tool_call_id="a"),
            ToolMessage(content="two", tool_call_id="b"), AIMessage("Found elves")]
        current = HumanMessage("And costing one?")
        budget = sum(message_size(message) for message in [*recent, current])
        messages = [*old, *recent]
        self.assertEqual(select_history(messages, current, budget), [*recent, current])
        self.assertEqual(len(messages), 7)
        self.assertEqual(select_history(messages, current, budget - 1), [current])
        self.assertEqual(select_history(recent[:-2], current, budget), [current])
        with self.assertRaises(ValueError):
            select_history(messages, current, message_size(current) - 1)

    def test_rules_checkpoint_types_reset_and_full_history_survives_trimming(self):
        """Reload typed rules evidence, then clear it while retaining every turn."""
        with TemporaryDirectory() as directory:
            database = Path(directory) / "rules.sqlite"
            workflow, llm = self.workflow(database)
            rule = SearchResult("100.1", "rule", "General", "Rule evidence", 1, 1, "test",
                                references=[Reference("rule", "100.2", "100.2", True)])
            workflow.retriever.search.return_value = [rule]
            workflow.retriever.get.return_value = []
            llm.generate.side_effect = [
                RouteDecision(language="en", english_query="Explain rules", route="rules",
                              card_names=[], clarification=""),
                Decomposition(queries=["rules"]),
                AnswerDraft(can_answer=True, claims=[Claim(text="Evidence.", source_ids=["100.1"])],
                            assumptions=[]),
                AnswerReview(supported=True, unsupported_claims=[], missing_information=[],
                             followup_queries=[])]
            workflow.answer("Explain rules", conversation_id="rules")
            restarted, llm = self.workflow(database)
            restarted.history_budget = message_size(HumanMessage("Next")) + 10
            llm.generate.side_effect = None
            llm.generate.return_value = RouteDecision(language="en", english_query="Next",
                route="clarify", card_names=[], clarification="Please clarify.")
            with restarted:
                loaded = restarted.graph.get_state({"configurable": {"thread_id": "rules"}}).values
                self.assertIsInstance(loaded["context"][0], SearchResult)
                self.assertIsInstance(loaded["context"][0].references[0], Reference)
                self.assertIsInstance(loaded["draft"], AnswerDraft)
                self.assertIsInstance(loaded["citations"][0], Citation)
                for _ in range(4):
                    result = restarted.answer("Next", conversation_id="rules")
                    self.assertEqual(result["history"], [])
                    self.assertEqual(result["context"], [])
                    self.assertIsNone(result["draft"])
                    self.assertEqual(result["reviews"], [])
                self.assertEqual(len(result["messages"]), 10)
                self.assertNotIn("Rule evidence", llm.generate.call_args.args[0])

    def test_same_conversation_turns_are_serialized(self):
        """A waiting turn enters only after the current owner releases the lock."""
        attempted, entered = Event(), Event()

        def wait_for_turn():
            attempted.set()
            with conversation_turn("history.sqlite", "same"):
                entered.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with conversation_turn("history.sqlite", "same"):
                future = executor.submit(wait_for_turn)
                self.assertTrue(attempted.wait(2))
                self.assertFalse(entered.is_set())
                # Another conversation can progress while this one is occupied.
                with conversation_turn("history.sqlite", "different"):
                    pass
            future.result(timeout=2)
        self.assertTrue(entered.is_set())
