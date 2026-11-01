"""Exercise native tool messages, validation and bounded execution without APIs."""

import json
import unittest
from unittest.mock import Mock

import httpx
from langchain_core.messages import AIMessage, ToolMessage

from backend.cards.agent import CardAgent
from backend.cards.mtg_api import MtgApiClient
from backend.cards.replies import CardReply
from backend.generation.llm import LLMConfig, StructuredOutputError


def call(name="get_card_by_name", args=None, identifier="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {"name": "Sample"}, "id": identifier}])


def reply(names=None):
    return CardReply(language="en", needs_clarification=False, text="Result.", card_names=names or [])


class CardToolTests(unittest.TestCase):
    def agent(self, messages, handler=None, final=None, **limits):
        def unexpected(_):
            self.fail("Rejected calls must not reach HTTP")
        http = httpx.Client(transport=httpx.MockTransport(handler or unexpected))
        self.addCleanup(http.close)
        api = MtgApiClient(client=http, retries=0, max_pages=1)
        llm = Mock(config=LLMConfig())
        llm.call_tools.side_effect = messages
        llm.generate.return_value = final or reply()
        return CardAgent(api, llm=llm, **limits), llm

    def test_native_cycle_retains_messages_and_http_evidence(self):
        agent, llm = self.agent([call(), AIMessage(content="Ready.")],
            lambda _: httpx.Response(200, json={"cards": [{"name": "Sample", "id": "p1"}]}), reply(["Sample"]))
        result = agent.answer("Find Sample")
        self.assertEqual(result.cards[0]["printings"][0]["id"], "p1")
        history = llm.call_tools.call_args_list[1].args[0]
        self.assertIsInstance(history[-1], ToolMessage)
        self.assertEqual(history[-1].tool_call_id, "call-1")
        self.assertEqual(json.loads(history[-1].content), result.exchanges[0].result)
        self.assertEqual({tool.name for tool in llm.call_tools.call_args.args[1]},
                         {"search_cards", "get_card_by_name"})

    def test_invalid_tools_and_arguments_never_reach_http(self):
        for message in [call("delete_cards"), call(args={"name": "Sample", "url": "bad"}),
                        call("search_cards", {"color": "purple"})]:
            with self.subTest(message=message):
                agent, _ = self.agent([message], max_calls=1)
                self.assertEqual(agent.answer("Find Sample").exchanges[0].result["status"], "rejected")

    def test_budget_stops_tool_loop_and_rejects_oversized_requests(self):
        agent, llm = self.agent([call()], lambda _: httpx.Response(200, json={"cards": []}), max_calls=1)
        agent.answer("Find Sample")
        self.assertEqual(llm.call_tools.call_count, 1)
        oversized = AIMessage(content="", tool_calls=[
            {"name": "get_card_by_name", "args": {"name": "Sample"}, "id": key} for key in ("a", "b")])
        agent, _ = self.agent([oversized], max_calls=1)
        with self.assertRaises(StructuredOutputError):
            agent.answer("Find Sample")

    def test_ambiguity_forces_clarification_and_stops_tools(self):
        agent, llm = self.agent([call(args={"name": "Ejemplo", "language": "Spanish"})],
            lambda _: httpx.Response(200, json={"cards": [
                {"name": name, "foreignNames": [{"name": "Ejemplo", "language": "Spanish"}]}
                for name in ("First", "Second")]}))
        result = agent.answer("Find Ejemplo")
        self.assertTrue(result.reply.needs_clarification)
        self.assertIn("Second", result.reply.text)
        self.assertEqual(llm.call_tools.call_count, 1)

    def test_final_reply_requires_catalogue_evidence(self):
        agent, _ = self.agent([AIMessage(content="I know it.")], final=reply(["Invented"]))
        with self.assertRaises(StructuredOutputError):
            agent.answer("Find Sample")
