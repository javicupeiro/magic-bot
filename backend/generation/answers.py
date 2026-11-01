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
from backend.common.prompts import PROMPTS, load_prompt
from backend.ingestion.ingest import DEFAULT_INPUT
from backend.retrieval.hybrid import DEFAULT_CANDIDATES
from backend.retrieval.retrieval import DEFAULT_K, DEFAULT_METHOD, METHODS, Retriever, SearchResult


class StructuredModel(BaseModel):
    """Reject undeclared fields in model output rather than silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class NormalizedQuestion(StructuredModel):
    """Keep the reply language while providing English text for rulebook retrieval."""

    language: Literal["en", "es"]
    english_query: str = Field(min_length=1)


class Claim(StructuredModel):
    """One answer statement with IDs of the retrieved sources supporting it."""

    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class AnswerDraft(StructuredModel):
    """Model proposal, checked locally before it becomes a user-facing answer."""

    can_answer: bool
    claims: list[Claim]
    assumptions: list[str]


class Citation(StructuredModel):
    """Evidence copied from storage, including its physical pages and edition."""

    source_id: str
    kind: str
    title: str
    excerpt: str
    page_start: int
    page_end: int
    version: str


class Answer(StructuredModel):
    """Final answer plus evidence, abstention state and generation provenance."""

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
    """Check reference membership and build citations solely from stored records.

    This checks IDs, evidence presence and output structure; it does not prove
    that a source logically supports a claim. Repeated IDs produce one citation.
    """
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
            # Render markers ourselves so model-written markers cannot bypass ID checks.
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


def finish_answer(draft: AnswerDraft, context: list[SearchResult], language: str,
                  reason: str | None = None) -> dict:
    """Validate and render a proposal; return an abstention on invalid citations.

    validation_error preserves the exact local rejection for evidence reviewers.
    This function performs no model calls and never mutates the original draft.
    """
    validation_error = None
    try:
        citations = validate_citations(draft, context)
    except ValueError as exc:
        # Preserve the deterministic rejection for callers that review the proposal.
        validation_error = str(exc)
        reason = "invalid_citations_or_answer"
        draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
        citations = []
    if draft.can_answer:
        text = "\n\n".join(claim.text + " " + " ".join(
            f"[{identifier}]" for identifier in dict.fromkeys(claim.source_ids))
            for claim in draft.claims)
        reason = None
    else:
        text = ("No puedo responder con la evidencia disponible en el documento."
                if language == "es" else "I cannot answer with the evidence available in the document.")
        reason = reason or "insufficient_evidence"
    return {"draft": draft, "citations": citations, "answer": text, "reason": reason,
            "validation_error": validation_error}


class AnswerService:
    """Normalize a question, retrieve evidence and validate a structured draft."""

    def __init__(self, retriever: Retriever, *, llm: LLM | None = None,
                 prompt_root: Path = PROMPTS):
        """Keep dependencies; load each active prompt only when its step executes."""
        self.retriever = retriever
        self.llm = llm if llm is not None else LLM()
        self.prompt_root = prompt_root

    def answer(self, question: str, *, k: int = DEFAULT_K, version: str | None = None) -> Answer:
        """Answer in English or Spanish using at most k sources of each queried kind.

        Preserve the original question and retrieved records for inspection.
        Invalid drafts become explicit abstentions; provider/network errors still
        propagate to the caller rather than masquerading as missing evidence.
        """
        if not question.strip():
            raise ValueError("Question must not be empty")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        # JSON quoting keeps the original question as a distinct prompt value.
        question_json = json.dumps(question, ensure_ascii=False)
        normalize_prompt = load_prompt("normalize", root=self.prompt_root)
        prompts = {"normalize": dict(normalize_prompt.metadata())}
        normalized = self.llm.generate(
            normalize_prompt.render(question_json=question_json), NormalizedQuestion)
        if not normalized.english_query.strip():
            raise ValueError("Normalized query must not be empty")
        context = []
        # Retrieve rules and glossary separately so one kind cannot fill every slot.
        for kind in ("rule", "glossary"):
            context.extend(self.retriever.search(normalized.english_query, k=k,
                                                kind=kind, version=version))
        reason = None
        draft = AnswerDraft(can_answer=False, claims=[], assumptions=[])
        citations = []
        if not context:
            # With no evidence there is no reason to make a second generation call.
            reason = "empty_context"
        else:
            answer_prompt = load_prompt("answer", root=self.prompt_root)
            prompts["answer"] = dict(answer_prompt.metadata())
            try:
                draft = self.llm.generate(answer_prompt.render(
                    question_json=question_json, language=normalized.language,
                    context_json=json.dumps([source.to_dict() for source in context], ensure_ascii=False),
                ), AnswerDraft)
            except StructuredOutputError:
                reason = "unavailable_structured_answer"
        finished = finish_answer(draft, context, normalized.language, reason)
        draft, citations = finished["draft"], finished["citations"]
        text, reason = finished["answer"], finished["reason"]
        return Answer(
            original_question=question, language=normalized.language,
            english_query=normalized.english_query, can_answer=draft.can_answer,
            answer=text, claims=draft.claims, source_ids=[citation.source_id for citation in citations],
            citations=citations, assumptions=draft.assumptions, abstention_reason=reason,
            retrieved_sources=[source.to_dict() for source in context],
            prompts=prompts,
            model=self.llm.config.model, reasoning_effort=self.llm.config.reasoning_effort,
        )


def main() -> None:
    """Print a full answer record, e.g. uv run --locked answer-rules 'What is mana?'"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="English or Spanish rules question")
    parser.add_argument("-k", type=int, default=DEFAULT_K, help="Results per source kind (rules and glossary)")
    parser.add_argument("--version", help="Exact rulebook edition")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD)
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT, help="Matching JSONL for BM25")
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATES)
    args = parser.parse_args()
    try:
        retriever = Retriever(args.db, args.collection, method=args.method,
                              source_path=args.source, candidate_limit=args.candidate_limit)
        result = AnswerService(retriever).answer(args.question, k=args.k, version=args.version)
    except OpenAIError as exc:
        parser.exit(1, f"OpenAI request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Answer generation failed: {exc}\n")
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
