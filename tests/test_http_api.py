"""Exercise HTTP/SSE with the real checkpointed graph and simulated dependencies."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from backend.api.app import create_app
from backend.cards.mtg_api import MtgApiClient
from backend.cards.replies import CardReply
from backend.generation.llm import LLMConfig
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.memory import conversation_turn
from backend.workflow.state import RouteDecision


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class HttpApiTests(unittest.TestCase):
    """Protect stream order/privacy, durable history and controlled rejection paths."""

    def setUp(self):
        environment = patch.dict("os.environ", {"LANGFUSE_TRACING_ENABLED": "false"})
        environment.start()
        self.addCleanup(environment.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "history.sqlite"
        self.llm = Mock(config=LLMConfig())
        self.llm.generate.side_effect = [
            RouteDecision(language="en", english_query="Find Sample", route="cards", card_names=[], clarification=""),
            CardReply(language="en", text="Found Sample.", needs_clarification=False, card_names=["Sample"])]
        model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "get_card_by_name", "id": "private-call-id", "args": {"name": "Sample"}}]),
            AIMessage(content="private model prose", additional_kwargs={"encrypted_content": "SECRET"},
                      usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})])
        self.llm.call_tools.side_effect = lambda messages, tools: model.invoke(messages)
        self.calls = 0

        def factory():
            self.calls += 1
            http = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
                "cards": [{"name": "Sample", "text": "First strike", "private": "SECRET"}]})))
            self.addCleanup(http.close)
            api = MtgApiClient(client=http, max_pages=1, retries=0)
            return QuestionWorkflow(Mock(), llm=self.llm, memory_path=self.database, api_factory=lambda: api)

        self.app = create_app(memory_path=self.database, workflow_factory=factory)

    def test_stream_native_subgraph_order_final_result_and_restart_history(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/conversations").json(), [])
            identifier = client.post("/conversations").json()["conversation_id"]
            empty_identifier = client.post("/conversations").json()["conversation_id"]
            conversations = [{"conversation_id": identifier}, {"conversation_id": empty_identifier}]
            self.assertEqual(client.get("/conversations").json(), conversations)
            path = f"/conversations/{identifier}/messages"
            self.assertEqual(client.get(path).json()["messages"], [])
            response = client.post(path, json={"message": "Find Sample"})
            self.assertEqual(response.status_code, 200)
            received = events(response)
            self.assertEqual(received[0]["type"], "turn_started")
            self.assertEqual(received[-1]["type"], "answer")
            self.assertEqual([item["sequence"] for item in received], list(range(1, len(received) + 1)))
            self.assertEqual(len({item["turn_id"] for item in received}), 1)
            started = [item for item in received if item["type"] == "step_started"]
            completed = [item for item in received if item["type"] == "step_completed"]
            self.assertEqual([item["step_id"] for item in started if item["step"] != "cards"],
                             [item["step_id"] for item in completed if item["step"] != "cards"])
            self.assertEqual({item["step_id"] for item in started}, {item["step_id"] for item in completed})
            card_starts = [item for item in started if item["step"] == "cards"]
            card_completions = [item for item in completed if item["step"] == "cards"]
            self.assertEqual(len(card_starts), 1)
            self.assertEqual(len(card_completions), 1)
            self.assertEqual(card_starts[0]["step_id"], card_completions[0]["step_id"])
            # Exercise real LangGraph callbacks: a scripted UI fixture cannot
            # catch a parent subgraph being mistaken for one of its children.
            children = [item for item in received if item.get("step", "").startswith("cards.")]
            self.assertTrue(all(card_starts[0]["sequence"] < item["sequence"]
                                < card_completions[0]["sequence"] for item in children))
            self.assertEqual([item["step"] for item in started if not item["step"].startswith("cards.")],
                             ["route", "cards", "check_cards", "remember"])
            self.assertIn("cards.tools", [item["step"] for item in completed])
            tools = [item for item in received if item["type"] == "tool"]
            self.assertEqual([item["status"] for item in tools], ["started", "completed"])
            self.assertEqual(tools[0]["step"], "cards.tools")
            usage = [item for item in received if item["type"] == "model_usage"]
            self.assertEqual(usage[0]["total_tokens"], 14)
            self.assertEqual(usage[0]["step"], "cards.model")
            result = received[-1]["result"]
            self.assertEqual(result["answer"], "Found Sample.")
            self.assertEqual(result["cards"][0]["name"], "Sample")
            self.assertEqual(result["sources"], [])
            for secret in ("SECRET", "private model prose", "private-call-id", "tool_messages", "namespace"):
                self.assertNotIn(secret, response.text)
            self.assertEqual(self.calls, 1)
        with TestClient(create_app(memory_path=self.database)) as restarted:
            self.assertEqual(restarted.get("/conversations").json(), conversations)
            messages = restarted.get(path).json()["messages"]
            self.assertEqual([item["content"] for item in messages], ["Find Sample", "Found Sample."])

    def test_contract_busy_and_stream_failure(self):
        with TestClient(self.app) as client:
            identifier = client.post("/conversations").json()["conversation_id"]
            path = f"/conversations/{identifier}/messages"
            for body in ({}, {"message": " "}, {"message": "ok", "callbacks": []}, {"message": "x" * 16001}):
                self.assertEqual(client.post(path, json=body).status_code, 422)
            self.assertEqual(client.post(f"/conversations/{uuid4()}/messages", json={"message": "hi"}).status_code, 404)
            self.assertEqual(client.get("/conversations/invalid/messages").status_code, 422)
            with conversation_turn(self.database, identifier):
                response = client.post(path, json={"message": "Find Sample"})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["detail"]["code"], "conversation_busy")
            self.llm.generate.side_effect = RuntimeError("SECRET provider body")
            response = client.post(path, json={"message": "fail"})
            received = events(response)
            self.assertEqual(received[-1]["type"], "error")
            self.assertNotIn("answer", [item["type"] for item in received])
            self.assertNotIn("SECRET", response.text)
            self.assertEqual(client.get(path).json()["messages"], [])
            self.llm.generate.side_effect = None
            self.llm.generate.return_value = RouteDecision(language="en", english_query="unclear", route="clarify",
                                                           card_names=[], clarification="Please clarify.")
            self.assertEqual(events(client.post(path, json={"message": "retry explicitly"}))[-1]["type"], "answer")

    def test_active_turn_rejects_second_post_while_history_remains_responsive(self):
        entered, release = Event(), Event()

        def generate(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test did not release model")
            return RouteDecision(language="en", english_query="unclear", route="clarify", card_names=[],
                                 clarification="Please clarify.")

        self.llm.generate.side_effect = generate
        with TestClient(self.app) as client, ThreadPoolExecutor(max_workers=1) as executor:
            identifier = client.post("/conversations").json()["conversation_id"]
            path = f"/conversations/{identifier}/messages"
            pending = executor.submit(client.post, path, json={"message": "first"})
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(client.post(path, json={"message": "second"}).status_code, 409)
                self.assertEqual(client.get(path).status_code, 200)
            finally:
                release.set()
            self.assertEqual(events(pending.result(timeout=5))[-1]["type"], "answer")
