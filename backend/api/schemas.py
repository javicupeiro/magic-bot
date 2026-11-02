"""Allowlisted HTTP and SSE records; graph/provider objects never cross this boundary."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.generation.answers import Citation


class ApiHelp(BaseModel):
    """Small entry point linking the local API's basic operations."""

    name: str
    message: str
    links: dict[str, str]


class ApiHealth(BaseModel):
    status: Literal["ok"]


class ApiVersion(BaseModel):
    name: str
    version: str


class ChatRequest(BaseModel):
    """Accept only user prose, with a bounded request size and no graph settings."""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=16000)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class PublicCard(BaseModel):
    """Catalogue presentation fields, excluding original provider printing bodies."""

    name: str
    mana_cost: str | None = None
    cmc: float | None = None
    colors: list[str] = Field(default_factory=list)
    color_identity: list[str] = Field(default_factory=list)
    text: str | None = None
    image_url: str | None = None


class PublicAnswer(BaseModel):
    """Final prose and validated evidence from the same graph execution."""

    answer: str
    cards: list[PublicCard]
    sources: list[Citation]


class PublicEvent(BaseModel):
    """Small discriminated event vocabulary with only explicit public fields."""

    type: Literal["turn_started", "step_started", "step_completed", "tool", "sources",
                  "model_usage", "answer", "error"]
    conversation_id: str
    turn_id: str
    sequence: int
    step: str | None = None
    step_id: str | None = None
    duration_ms: float | None = None
    tool: str | None = None
    status: Literal["started", "completed", "failed"] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    sources: list[Citation] | None = None
    result: PublicAnswer | None = None
    code: str | None = None
    message: str | None = None


class Conversation(BaseModel):
    conversation_id: str


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ConversationHistory(Conversation):
    messages: list[HistoryMessage]
