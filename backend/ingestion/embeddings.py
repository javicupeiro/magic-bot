"""Explicit OpenAI embedding configuration and provider adapter."""

from dataclasses import dataclass
import math
import os

from dotenv import load_dotenv
from openai import OpenAI


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str = "text-embedding-3-small"
    dimensions: int = 1536

    def __post_init__(self):
        if not self.model.strip() or self.dimensions <= 0:
            raise ValueError("Embedding model must be nonempty and dimensions positive")

    @classmethod
    def from_env(cls):
        load_dotenv()
        return cls(
            model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
            dimensions=int(os.getenv("OPENAI_EMBEDDING_DIMENSIONS", "1536")),
        )


def validate_vectors(vectors, count: int, dimensions: int):
    if len(vectors) != count or any(
        len(vector) != dimensions or not all(math.isfinite(value) for value in vector)
        or not any(vector) for vector in vectors
    ):
        raise ValueError("Provider returned invalid embedding count, dimensions or values")


class OpenAIEmbeddings:
    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self._client = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Initialize lazily: a fully cached run needs no API key or network.
        if self._client is None:
            load_dotenv()
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                raise ValueError("OPENAI_API_KEY is required for uncached embeddings")
            self._client = OpenAI(api_key=key)
        response = self._client.embeddings.create(
            model=self.config.model, dimensions=self.config.dimensions,
            input=texts, encoding_format="float",
        )
        items = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(texts))):
            raise ValueError("Provider returned invalid embedding indexes")
        vectors = [item.embedding for item in items]
        validate_vectors(vectors, len(texts), self.config.dimensions)
        return vectors
