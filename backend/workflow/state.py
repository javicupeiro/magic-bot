"""Describe the data exchanged by workflow nodes and structured model outputs.

RouteDecision and Decomposition are Pydantic models: incoming model responses are
validated against their fields and constraints. WorkflowState is a TypedDict:
it describes the graph's channels for typing, not a runtime Pydantic validator.
Each node returns only changed fields; other values remain available downstream.
Evidence, tool history and final prose have separate fields for inspection.
"""

from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage
from pydantic import Field

from backend.cards.replies import CardReply
from backend.generation.answers import AnswerDraft, Citation, StructuredModel
from backend.retrieval.retrieval import SearchResult


class RouteDecision(StructuredModel):
    """Structured classification returned by the route prompt.

    english_query supports retrieval against the English rulebook. card_names
    keeps proper names as provided so localized names can be verified by tools.
    clarification is user-facing text for the clarify route, empty otherwise.
    """

    language: Literal["en", "es"]
    english_query: str = Field(min_length=1)
    route: Literal["rules", "cards", "clarify"]
    card_names: list[str]
    clarification: str


class Decomposition(StructuredModel):
    """One to three English queries proposed by the decomposition prompt.

    Pydantic enforces the list length. The node separately rejects blank strings
    and removes duplicates. A simple definition is instructed to use one query.
    """

    queries: list[str] = Field(min_length=1, max_length=3)


class WorkflowState(TypedDict, total=False):
    """Invocation-local records; no persistent conversation or autonomous loop.

    total=False allows fields to appear as nodes produce them. With no custom
    reducers, a returned field replaces its old value rather than appending.

    tool_messages retains native LangChain messages from the cards subgraph.
    Provider protocol details are handled by the model integration.
    """

    # Generation provenance and the original, unmodified user input.
    model: str
    reasoning_effort: str
    question: str
    decision: RouteDecision
    queries: list[str]
    # All catalogue records remain inspectable, including incomplete lookups.
    cards: list[dict]
    # Rules reasoning only receives cards from complete successful exact lookups.
    verified_cards: list[dict]
    # Native card subgraph messages and structured evidence.
    exchanges: list[dict]
    tool_messages: list[BaseMessage]
    card_names: list[str]
    card_reply: CardReply
    card_prompts: dict
    # Stored rulebook evidence, model proposal, locally checked citations, prose.
    context: list[SearchResult]
    draft: AnswerDraft
    citations: list[Citation]
    answer: str
    # Terminal conditions and diagnostics explain why a path stopped or abstained.
    blocked: bool
    reason: str | None
    trace: list[dict]
    prompts: dict[str, dict]
