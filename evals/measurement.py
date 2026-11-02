"""Shared evaluation instrumentation, snapshots and reference validation.

Wrappers forward to the real dependencies. Controlled probes are opt-in and used
only by review experiments; delivery always supplies probe=None.
"""

import json
import math
from statistics import mean
from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler

from backend.generation.answers import AnswerDraft, Claim


class UsageRecorder(BaseCallbackHandler):
    """Retain provider-reported token counts only, without message/reasoning bodies."""

    def __init__(self):
        self.records = []

    def on_llm_end(self, response, **kwargs):
        """Read native message usage when available; absence stays distinguishable."""
        for batch in response.generations:
            for generation in batch:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    self.records.append({key: usage[key] for key in
                                         ("input_tokens", "output_tokens", "total_tokens")})


class MeasuredModel:
    """Delegate to the existing LLM while counting logical calls and optional probes."""

    def __init__(self, llm, probe, usage):
        self.llm, self.config, self.probe, self.usage = llm, llm.config, probe, usage
        self.calls = []
        self.injected = False

    def _call(self, operation, request):
        """Measure one application invocation, retaining failures but no private data."""
        started = perf_counter()
        before = len(self.usage.records)
        record = {"operation": operation, "status": "error"}
        try:
            result = request()
            record["status"] = "ok"
            return result
        finally:
            record["elapsed_ms"] = (perf_counter() - started) * 1000
            record["usage"] = self.usage.records[before:]
            self.calls.append(record)

    def generate(self, prompt, output_type):
        """Inject a known defect only in the first successful answer, never in review."""
        result = self._call(output_type.__name__, lambda: self.llm.generate(prompt, output_type))
        if output_type is AnswerDraft and result.can_answer and result.claims and not self.injected:
            if self.probe in {"unsupported_claim", "invalid_citation"}:
                result = result.model_copy(deep=True)
                if self.probe == "unsupported_claim":
                    result.claims.append(Claim(
                        text="First strike also doubles all damage the creature deals.",
                        source_ids=list(result.claims[0].source_ids)))
                else:
                    result.claims[0].source_ids = ["unretrieved-evaluation-source"]
                self.injected = True
        return result

    def call_tools(self, messages, tools):
        """Count card-model calls separately from the structured rules calls."""
        return self._call("card_tools", lambda: self.llm.call_tools(messages, tools))


class MeasuredRetriever:
    """Forward the retrieval contract, recording queries and exact lookups separately."""

    def __init__(self, retriever, probe):
        self.retriever = retriever
        self.hide_initial = probe == "empty_initial_context"
        self.calls = []

    def _call(self, operation, value, options):
        """The empty-context probe withholds evidence until the first review ends."""
        started = perf_counter()
        results = [] if self.hide_initial else getattr(self.retriever, operation)(value, **options)
        self.calls.append({"operation": operation, "value": value, "options": options,
                           "withheld": self.hide_initial,
                           "source_ids": [source.id for source in results],
                           "elapsed_ms": (perf_counter() - started) * 1000})
        return results

    def search(self, query, **options):
        """Record each kind-specific search, not just each recovery round."""
        return self._call("search", query, options)

    def get(self, identifier, **options):
        """Exact reference lookups do not count as embedding searches."""
        return self._call("get", identifier, options)


def snapshot(state, model, retriever, started):
    """Freeze an inspectable answer and cumulative resource measurements as JSON data."""
    usage = [entry for call in model.calls for entry in call["usage"]]
    evidence = {"rules": [source.to_dict() for source in state["context"]],
                "verified_cards": state["verified_cards"]}
    return {"answer": state["answer"], "can_answer": bool(state.get("draft") and state["draft"].can_answer),
            "draft": state.get("draft"), "candidate_draft": state.get("candidate_draft"),
            "reason": state.get("reason"), "validation_error": state.get("validation_error"),
            "evidence": evidence, "citations": state["citations"],
            "context_characters": len(json.dumps(evidence, ensure_ascii=False)),
            "model_calls": len(model.calls),
            "search_calls": sum(call["operation"] == "search" for call in retriever.calls),
            "real_search_calls": sum(call["operation"] == "search" and not call["withheld"]
                                     for call in retriever.calls),
            "exact_lookups": sum(call["operation"] == "get" for call in retriever.calls),
            "calls_with_usage": sum(bool(call["usage"]) for call in model.calls),
            "input_tokens": sum(item["input_tokens"] for item in usage),
            "output_tokens": sum(item["output_tokens"] for item in usage),
            "elapsed_ms": (perf_counter() - started) * 1000}


def validate_cases(bank, retriever):
    """Validate the fixed bank and reference sources before paid requests begin."""
    seen = set()
    for case in bank["cases"]:
        if case["id"] in seen or not case["question"].strip() or not case["criteria"]:
            raise ValueError("Cases need unique IDs, a question and predeclared criteria")
        if case["expectation"] not in {"answer", "abstain", "clarify"}:
            raise ValueError("Unknown expected outcome")
        if case["cohort"] not in {"natural", "controlled"}:
            raise ValueError("Unknown case cohort")
        if case.get("probe") not in {None, "unsupported_claim", "invalid_citation", "empty_initial_context"}:
            raise ValueError("Unknown controlled probe")
        seen.add(case["id"])
        for identifier in case["reference_ids"]:
            if not retriever.get(identifier, version=bank["version"]):
                raise ValueError(f"Missing reference source: {identifier}")
    if not seen:
        raise ValueError("The case bank must not be empty")


def distribution(values):
    """Summarize observed values; percentiles use the nearest observed rank."""
    if not values:
        return None
    ordered = sorted(values)
    return {"mean": mean(values), "p50": ordered[math.ceil(len(values) * .5) - 1],
            "p95": ordered[math.ceil(len(values) * .95) - 1], "max": ordered[-1]}
