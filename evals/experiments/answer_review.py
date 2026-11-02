"""Run paired review experiments through the real workflow and save local JSON.

This CLI makes paid model/embedding calls and optionally catalogue HTTP requests.
It captures the first validated draft, the first review's stop decision and the
actual final result from one execution. Probes alter only evaluation dependencies;
production prompts, nodes, routing and defaults are never replaced.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import random
from time import perf_counter

import chromadb
from openai import OpenAIError

from backend.cards.mtg_api import MtgApiClient
from backend.generation.answers import AnswerDraft, finish_answer
from backend.generation.llm import LLM
from backend.ingestion.ingest import DEFAULT_INPUT
from backend.retrieval.hybrid import DEFAULT_CANDIDATES
from backend.retrieval.retrieval import DEFAULT_K, DEFAULT_METHOD, METHODS, Retriever
from backend.workflow.cli import _serialize
from backend.workflow.graph import QuestionWorkflow, initial_state
from evals.experiments.review_metrics import summarize
from evals.measurement import MeasuredModel, MeasuredRetriever, UsageRecorder, snapshot, validate_cases


def run_case(case, retriever, llm, *, repetition, usage=None, **settings):
    """Execute a case once; three snapshots share the exact initial draft/evidence.

    review_only is a counterfactual stop after the first verdict, using the same
    finish_answer policy; it makes no extra calls and is not another graph run.
    Non-rules terminal paths get identical snapshots and no invented judge calls.
    """
    usage = usage or UsageRecorder()
    model = MeasuredModel(llm, case.get("probe"), usage)
    measured = MeasuredRetriever(retriever, case.get("probe"))
    workflow = QuestionWorkflow(measured, llm=model, **settings)
    state = initial_state(case["question"], llm)
    variants = {}
    nodes = []
    repair_queries = []
    started = perf_counter()
    for namespace, update in workflow.stream(case["question"]):
        if namespace:
            continue
        for node, values in update.items():
            state.update(values)
            nodes.append(node)
            if node == "validate" and "baseline" not in variants:
                variants["baseline"] = snapshot(state, model, measured, started)
            if node == "review" and "review_only" not in variants:
                stopped = dict(state)
                if not state["reviews"][-1]["approved"]:
                    stopped.update(finish_answer(AnswerDraft(can_answer=False, claims=[], assumptions=[]),
                                                 state["context"], state["decision"].language,
                                                 state.get("reason") or "unsupported_answer"))
                variants["review_only"] = snapshot(stopped, model, measured, started)
                # The following node can now access normal evidence for the probe.
                measured.hide_initial = False
                repair_queries = list(state["repair_queries"])
    variants["final"] = snapshot(state, model, measured, started)
    variants.setdefault("baseline", variants["final"])
    variants.setdefault("review_only", variants["final"])
    # Serialize now: later graph/model activity cannot mutate earlier snapshots.
    sample = {"sample_id": f"{case['id']}:r{repetition}", "case_id": case["id"],
              "question": case["question"], "cohort": case["cohort"],
              "expectation": case["expectation"], "probe": case.get("probe"),
              "probe_applied": model.injected or any(call["withheld"] for call in measured.calls),
              "repetition": repetition, "status": "ok", "nodes": nodes,
              "reviews": state["reviews"], "prompts": state.get("prompts", {}),
              "additional_retrievals": state["additional_retrievals"],
              "executed_repair_queries": repair_queries if state["additional_retrievals"] else [],
              "context_budget_exceeded": any(v["context_characters"] > settings.get("context_budget", 24000)
                                             for v in variants.values()),
              "model_calls": model.calls, "retrieval_calls": measured.calls,
              "variants": variants}
    return json.loads(json.dumps(sample, default=_serialize, ensure_ascii=False))


def main():
    """Run shuffled repeated cases and checkpoint after each completed/failed sample."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("data/answer_review/cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATES)
    parser.add_argument("-k", type=int, default=DEFAULT_K)
    parser.add_argument("--context-budget", type=int, default=24000)
    args = parser.parse_args()
    if args.repeats < 1 or args.k < 1 or args.context_budget < 100:
        parser.error("repeats/k must be positive; context-budget must be at least 100")
    if args.output.exists():
        parser.error("output already exists; choose a new path to preserve the experiment")
    raw_cases = args.cases.read_bytes()
    bank = json.loads(raw_cases)
    retriever = Retriever(args.db, args.collection, method=args.method,
                          source_path=args.source, candidate_limit=args.candidate_limit)
    validate_cases(bank, retriever)
    llm = LLM()
    usage = UsageRecorder()
    # Attach the standard callback only to this evaluation-owned client.
    llm.client.callbacks = [usage]
    schedule = []
    randomizer = random.Random(args.seed)
    for repetition in range(1, args.repeats + 1):
        order = list(bank["cases"])
        randomizer.shuffle(order)
        schedule.extend((repetition, case) for case in order)
    report = {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "cases_sha256": hashlib.sha256(raw_cases).hexdigest(), "bank": bank,
              "model": vars(llm.config), "collection": retriever.collection.name,
              "collection_count": retriever.collection.count(), "index_contract": retriever.contract,
              "settings": {"k": args.k, "method": args.method, "context_budget": args.context_budget,
                           "candidate_limit": args.candidate_limit, "repeats": args.repeats, "seed": args.seed,
                           "max_card_tool_calls": 4, "catalogue_max_pages": 1, "catalogue_retries": 0},
              "schedule": [f"{case['id']}:r{rep}" for rep, case in schedule], "samples": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition, case in schedule:
        try:
            sample = run_case(case, retriever, llm, repetition=repetition, usage=usage,
                              k=args.k, version=bank["version"], context_budget=args.context_budget,
                              api_factory=lambda: MtgApiClient(max_pages=1, retries=0))
        except (OpenAIError, OSError, ValueError, chromadb.errors.ChromaError) as exc:
            # Do not save exception bodies that might include provider request data.
            sample = {"sample_id": f"{case['id']}:r{repetition}", "case_id": case["id"],
                      "cohort": case["cohort"], "status": "error", "error_type": type(exc).__name__}
        report["samples"].append(sample)
        report["summary"] = summarize(report["samples"])
        temporary = args.output.with_suffix(args.output.suffix + ".partial")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(args.output)
        print(f"{sample['sample_id']}: {sample['status']}", flush=True)


if __name__ == "__main__":
    main()
