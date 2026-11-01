"""Structured generation through the OpenAI Responses API."""

from dataclasses import dataclass
import os
from typing import TypeVar

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel

Output = TypeVar("Output", bound=BaseModel)


class StructuredOutputError(ValueError):
    """The provider refused, stopped early, or returned no structured output."""


@dataclass(frozen=True)
class LLMConfig:
    model: str = "gpt-6-luna"
    reasoning_effort: str = "high"

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("OPENAI_LLM_MODEL must not be empty")
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Invalid OPENAI_REASONING_EFFORT")

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(model=os.getenv("OPENAI_LLM_MODEL", "gpt-6-luna"),
                   reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "high"))


class LLM:
    def __init__(self, config: LLMConfig | None = None, *, client=None):
        self.config = config if config is not None else LLMConfig.from_env()
        self._client = client

    def generate(self, prompt: str, output_type: type[Output]) -> Output:
        if self._client is None:
            load_dotenv()
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY is required for answer generation")
            self._client = OpenAI(api_key=key)
        response = self._client.responses.parse(
            model=self.config.model,
            reasoning={"effort": self.config.reasoning_effort},
            input=[{"role": "user", "content": prompt}],
            text_format=output_type,
            store=False,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise StructuredOutputError("No completed structured response (refusal or incomplete output)")
        return response.output_parsed
