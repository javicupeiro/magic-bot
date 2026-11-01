"""Public card results, separate from LangGraph's execution state."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class CardReply(BaseModel):
    """Structured final output requested from the model after tool execution.

    card_names declares which canonical names the reply uses; the application
    checks them against complete HTTP evidence. Actual records live separately
    in CardAgentResult.cards, not inside model-generated prose.
    """

    model_config = ConfigDict(extra="forbid")
    language: Literal["en", "es"]
    needs_clarification: bool
    text: str = Field(min_length=1)
    # Canonical names must be present in successful HTTP evidence, not invented.
    card_names: list[str]


class ToolExchange(BaseModel):
    """Audit record linking a model request to the catalogue result returned to it.

    arguments serializes the model-selected argument dictionary. call_id links
    the AIMessage request to its ToolMessage result.
    """

    call_id: str
    name: str
    arguments: str
    result: dict


class CardAgentResult(BaseModel):
    """Public result of one question, with prose and evidence kept independent.

    cards merges original printings from all lookups, including partial ones.
    exchanges preserves per-call completeness/errors; model and prompt fields
    record generation provenance. No provider reasoning history is exposed here.
    """

    question: str
    reply: CardReply
    cards: list[dict]
    exchanges: list[ToolExchange]
    model: str
    reasoning_effort: str
    prompts: dict[str, dict[str, str]]
    max_calls: int
    max_pages: int
