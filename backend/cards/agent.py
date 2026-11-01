"""A bounded LangGraph tool cycle shared by card-chat and the main workflow.

prepare -> model -> tools -> collect -> model ... -> finish
Each box is a real graph node. stream(subgraphs=True) can expose them individually;
there is no hand-written provider loop or conversion between message protocols.
"""

import json
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError

from backend.cards.replies import CardAgentResult, CardReply, ToolExchange
from backend.cards.tools import catalogue_tools
from backend.common.prompts import load_prompt
from backend.generation.llm import LLM, StructuredOutputError


class CardState(TypedDict, total=False):
    """Only messages append; evidence fields are replaced with their latest value."""

    question: str
    card_names: list[str]
    tool_messages: Annotated[list[BaseMessage], add_messages]
    exchanges: list[dict]
    cards: list[dict]
    card_reply: CardReply
    card_prompts: dict


def tool_error(error: ValueError | ToolInvocationError) -> str:
    """Return validation failures to the model without disguising runtime bugs."""
    return json.dumps({"status": "rejected", "error": {"kind": "invalid_arguments"}})


def build_card_graph(api, llm, *, max_calls=4):
    """Build the single tool workflow; the caller owns the HTTP client's lifetime."""
    if type(max_calls) is not int or not 1 <= max_calls <= 10:
        raise ValueError("max_calls must be an integer between 1 and 10")
    tools = catalogue_tools(api)

    def prepare(state):
        """Load this turn's prompts and start a fresh native message history."""
        prompt = load_prompt("card_tools")
        messages = [SystemMessage(prompt.render(max_calls=str(max_calls), max_pages=str(api.max_pages)))]
        prompts = {prompt.name: dict(prompt.metadata())}
        if state.get("card_names"):
            resolver = load_prompt("resolve_cards")
            messages.append(SystemMessage(resolver.render(names_json=json.dumps(state["card_names"], ensure_ascii=False))))
            prompts[resolver.name] = dict(resolver.metadata())
        messages.append(HumanMessage(state["question"]))
        return {"tool_messages": messages, "cards": [], "exchanges": [], "card_prompts": prompts}

    def model(state):
        """Ask for tools and check call IDs/budget before allowing execution."""
        response = llm.call_tools(state["tool_messages"], tools)
        if not isinstance(response, AIMessage) or response.invalid_tool_calls:
            raise StructuredOutputError("Invalid model tool response")
        calls = response.tool_calls
        previous_ids = {item["call_id"] for item in state["exchanges"]}
        ids = [call["id"] for call in calls]
        if len(calls) > max_calls - len(previous_ids):
            raise StructuredOutputError("Model exceeded the tool call budget")
        if any(not value or value in previous_ids for value in ids) or len(set(ids)) != len(ids):
            raise StructuredOutputError("Missing or duplicate tool call ID")
        return {"tool_messages": [response]}

    def collect(state):
        """Associate tool results with their requests and retain catalogue facts."""
        # ToolNode returns one ToolMessage per call. Original AI messages remain
        # untouched so the model adapter can preserve provider reasoning state.
        messages = state["tool_messages"]
        request = next(message for message in reversed(messages) if isinstance(message, AIMessage))
        results = {message.tool_call_id: message for message in messages if isinstance(message, ToolMessage)}
        exchanges = list(state["exchanges"])
        cards = {card["name"]: card for card in state["cards"]}
        for call in request.tool_calls:
            message = results[call["id"]]
            if message.status == "error":
                result = {"status": "rejected", "error": {"kind": "invalid_tool_call"}}
            else:
                result = json.loads(message.content)
            exchanges.append({"call_id": call["id"], "name": call["name"],
                              "arguments": json.dumps(call["args"], ensure_ascii=False), "result": result})
            for card in result.get("items", []):
                existing = cards.setdefault(card["name"], {**card, "printings": []})
                for printing in card["printings"]:
                    if printing not in existing["printings"]:
                        existing["printings"].append(printing)
        return {"exchanges": exchanges, "cards": list(cards.values())}

    def finish(state):
        """Format the evidence and reject names unsupported by complete lookups."""
        # The final structured reply is a separate visible model call, with no
        # tools attached. Exhausting the call budget cannot trigger more HTTP.
        prompt = load_prompt("card_reply")
        reply = llm.generate(prompt.render(question_json=json.dumps(state["question"], ensure_ascii=False),
                             evidence_json=json.dumps(state["exchanges"], ensure_ascii=False)), CardReply)
        verified = set()
        ambiguous = []
        for exchange in state["exchanges"]:
            result = exchange["result"]
            if result["status"] == "ok" and not result.get("partial") and not result.get("error"):
                verified.update(card["name"] for card in result.get("items", []))
            if result["status"] == "ambiguous":
                ambiguous.extend(card["name"] for card in result["items"])
        if ambiguous:
            reply.needs_clarification = True
            prefix = "¿A qué carta te refieres? Opciones: " if reply.language == "es" else "Which card do you mean? Options: "
            reply.text = prefix + ", ".join(dict.fromkeys(ambiguous))
            reply.card_names = []
        elif set(reply.card_names) - verified:
            raise StructuredOutputError("Reply references cards without HTTP evidence")
        return {"card_reply": reply, "card_prompts": {
            **state["card_prompts"], prompt.name: dict(prompt.metadata())}}

    def after_collect(state):
        """Continue only while budget remains and identity is not ambiguous."""
        ambiguous = any(item["result"]["status"] == "ambiguous" for item in state["exchanges"])
        return "finish" if ambiguous or len(state["exchanges"]) >= max_calls else "model"

    graph = StateGraph(CardState)
    for name, node in (("prepare", prepare), ("model", model),
                       ("tools", ToolNode(tools, messages_key="tool_messages", handle_tool_errors=tool_error)),
                       ("collect", collect), ("finish", finish)):
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "model")
    graph.add_conditional_edges("model", lambda state: "tools" if state["tool_messages"][-1].tool_calls else "finish", ["tools", "finish"])
    graph.add_edge("tools", "collect")
    graph.add_conditional_edges("collect", after_collect, ["model", "finish"])
    graph.add_edge("finish", END)
    return graph.compile()


class CardAgent:
    """CLI convenience wrapper around the same graph used by magic-chat."""

    def __init__(self, api, *, llm=None, max_calls=4):
        self.api = api
        self.llm = llm if llm is not None else LLM()
        self.max_calls = max_calls
        self.graph = build_card_graph(api, self.llm, max_calls=max_calls)

    def answer(self, question: str) -> CardAgentResult:
        if not question.strip():
            raise ValueError("Question must not be empty")
        state = self.graph.invoke({"question": question, "card_names": []}, {"recursion_limit": 64})
        return CardAgentResult(question=question, reply=state["card_reply"], cards=state["cards"],
            exchanges=[ToolExchange(**item) for item in state["exchanges"]],
            model=self.llm.config.model, reasoning_effort=self.llm.config.reasoning_effort,
            prompts=state["card_prompts"], max_calls=self.max_calls, max_pages=self.api.max_pages)
