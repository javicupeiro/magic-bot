"""Structured generation and model-selected tools through the Responses API."""

from dataclasses import dataclass
import os
from typing import TypeVar

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

Output = TypeVar("Output", bound=BaseModel)


class StructuredOutputError(ValueError):
    """The provider refused, stopped early, or returned no structured output."""


@dataclass(frozen=True)
class LLMConfig:
    """Explicit model settings recorded alongside every generated answer."""

    model: str = "gpt-6-luna"
    reasoning_effort: str = "high"

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("OPENAI_LLM_MODEL must not be empty")
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid OPENAI_REASONING_EFFORT")

    @classmethod
    def from_env(cls):
        """Read settings from the environment; .env fills only missing variables."""
        load_dotenv()
        return cls(model=os.getenv("OPENAI_LLM_MODEL", "gpt-6-luna"),
                   reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "high"))


class LLM:
    """Use one LangChain model for structured answers and native tool messages.

    ChatOpenAI owns the provider protocol, including Responses reasoning items.
    Application code only handles Pydantic outputs and LangChain messages.
    A fake chat model can be injected for tests without credentials or network.
    """

    def __init__(self, config: LLMConfig | None = None, *, client=None):
        self.config = config if config is not None else LLMConfig.from_env()
        self._client = client

    @property
    def client(self):
        """Initialize lazily so document-only commands need no API credentials."""
        if self._client is None:
            load_dotenv()
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY is required for answer generation")
            self._client = ChatOpenAI(
                api_key=key, model=self.config.model,
                reasoning={"effort": self.config.reasoning_effort},
                use_responses_api=True, output_version="responses/v1",
                store=False, include=["reasoning.encrypted_content"],
            )
        return self._client

    def generate(self, prompt: str, output_type: type[Output]) -> Output:
        """Parse a structured answer; refusals must not become valid evidence."""
        result = self.client.with_structured_output(
            output_type, method="json_schema", strict=True,
        ).invoke(prompt)
        if not isinstance(result, output_type):
            raise StructuredOutputError("No completed structured response")
        return result

    def call_tools(self, messages: list, tools: list):
        """Let the model request decorated tools; LangGraph executes them later."""
        return self.client.bind_tools(tools, parallel_tool_calls=False).invoke(messages)
