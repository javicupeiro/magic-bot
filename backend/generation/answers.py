"""Generate bilingual answers grounded in retrieved rulebook records."""

import argparse
import json
from pathlib import Path
import re
from typing import Literal

import chromadb
from openai import OpenAIError
from pydantic import BaseModel, ConfigDict, Field

from backend.generation.llm import LLM, StructuredOutputError
from backend.generation.prompt_loader import PROMPTS, load_prompt
from backend.retrieval.retrieval import Retriever, SearchResult


class StructuredModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NormalizedQuestion(StructuredModel):
    language: Literal["en", "es"]
    english_query: str = Field(min_length=1)


class Claim(StructuredModel):
    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class AnswerDraft(StructuredModel):
    can_answer: bool
    claims: list[Claim]
    assumptions: list[str]


class Citation(StructuredModel):
    source_id: str
    kind: str
    title: str
    excerpt: str
    page_start: int
    page_end: int
    version: str


class Answer(StructuredModel):
    original_question: str
    language: Literal["en", "es"]
    english_query: str
    can_answer: bool
    answer: str
    claims: list[Claim]
    source_ids: list[str]
    citations: list[Citation]
    assumptions: list[str]
    abstention_reason: str | None
    retrieved_sources: list[dict]
    prompts: dict[str, dict[str, str]]
    model: str
    reasoning_effort: str


def validate_citations(draft: AnswerDraft, context: list[SearchResult]) -> list[Citation]:
    """Reject unsupported references; construct citations solely from stored data."""
    if not draft.can_answer:
        if draft.claims or draft.assumptions:
            raise ValueError("An abstention must not contain claims or assumptions")
        return []
    if not draft.claims:
        raise ValueError("An answer requires supported claims")
    sources = {source.id: source for source in context}
    citations = {}
    for claim in draft.claims:
        if not claim.text.strip() or not claim.source_ids:
            raise ValueError("Each claim requires text and source IDs")
        if re.search(r"\[[^\]\n]+\]|mtg:", claim.text):
            raise ValueError("Citation markers must be constructed from source IDs, not claim text")
        for identifier in claim.source_ids:
            source = sources.get(identifier)
            if source is None:
                raise ValueError(f"Citation is outside retrieved context: {identifier}")
            if not source.text.strip():
                raise ValueError(f"Citation has no stored evidence: {identifier}")
            citations[identifier] = Citation(
                source_id=source.id, kind=source.kind, title=source.title,
                excerpt=source.text, page_start=source.page_start,
                page_end=source.page_end, version=source.version,
            )
    return list(citations.values())


class AnswerService:
    def __init__(self, retriever: Retriever, *, llm: LLM | None = None,
                 prompt_root: Path = PROMPTS):
        self.retriever = retriever
        self.llm = llm if llm is not None else LLM()
        self.normalize_prompt = load_prompt("normalize", root=prompt_root)
        self.answer_prompt = load_prompt("answer", root=prompt_root)

    def answer(self, question: str, *, k: int = 5, version: str | None = None) -> Answer:
        if not question.strip():
            raise ValueError("Question must not be empty")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        question_json = json.dumps(question, ensure_ascii=False)
        normalized = self.llm.generate(
            self.normalize_prompt.render(question_json=question_json), NormalizedQuestion)
        if not normalized.english_query.strip():
            raise ValueError("Normalized query must not be empty")
        context = []
        for kind in ("rule", "glossary"):
            context.extend(self.retriever.search(normalized.english_query, k=k,
                                                kind=kind, version=version))
        reason = None
        draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
        citations = []
        if not context:
            reason = "empty_context"
        else:
            try:
                draft = self.llm.generate(self.answer_prompt.render(
                    question_json=question_json, language=normalized.language,
                    context_json=json.dumps([source.to_dict() for source in context], ensure_ascii=False),
                ), AnswerDraft)
                citations = validate_citations(draft, context)
                if not draft.can_answer:
                    reason = "insufficient_evidence"
            except StructuredOutputError:
                reason = "unavailable_structured_answer"
            except ValueError:
                reason = "invalid_citations_or_answer"
        if reason is not None:
            draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
            citations = []
        text = "\n\n".join(
            claim.text + " " + " ".join(f"[{identifier}]" for identifier in dict.fromkeys(claim.source_ids))
            for claim in draft.claims)
        if not draft.can_answer:
            text = ("No puedo responder con la evidencia disponible en el documento."
                    if normalized.language == "es" else
                    "I cannot answer with the evidence available in the document.")
        return Answer(
            original_question=question, language=normalized.language,
            english_query=normalized.english_query, can_answer=draft.can_answer,
            answer=text, claims=draft.claims, source_ids=[citation.source_id for citation in citations],
            citations=citations, assumptions=draft.assumptions, abstention_reason=reason,
            retrieved_sources=[source.to_dict() for source in context],
            prompts={"normalize": dict(self.normalize_prompt.metadata()),
                     "answer": dict(self.answer_prompt.metadata())},
            model=self.llm.config.model, reasoning_effort=self.llm.config.reasoning_effort,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="English or Spanish rules question")
    parser.add_argument("-k", type=int, default=5, help="Results per source kind (rules and glossary)")
    parser.add_argument("--version", help="Exact rulebook edition")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    args = parser.parse_args()
    try:
        result = AnswerService(Retriever(args.db, args.collection)).answer(
            args.question, k=args.k, version=args.version)
    except OpenAIError as exc:
        parser.exit(1, f"OpenAI request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Answer generation failed: {exc}\n")
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
