"""Save retrieval results against a small set of manually reviewed sources."""

import argparse
from datetime import datetime, timezone
import json
import math
import random
from pathlib import Path
from time import perf_counter
from statistics import mean

import chromadb
from openai import OpenAIError

from backend.ingestion.ingest import DEFAULT_INPUT
from backend.retrieval.hybrid import DEFAULT_CANDIDATES, RRF_CONSTANT
from backend.retrieval.retrieval import DEFAULT_METHOD, METHODS, DISTANCE_EXPLANATION, Retriever


def summarize(runs: list[dict]) -> dict:
    """Aggregate query measurements; p95 uses the nearest observed rank.

    Relevance judgments are incomplete, so do not label unjudged sources false
    positives. MRR measures the first reviewed hit; full recall needs every one.
    """
    latencies = sorted(run["latency_ms"] for run in runs)
    return {"mean_recall_at_k": mean(run["reviewed_source_recall_at_k"] for run in runs),
            "hit_rate": mean(bool(run["matched_source_ids"]) for run in runs),
            "full_recall_rate": mean(run["reviewed_source_recall_at_k"] == 1 for run in runs),
            "mean_reciprocal_rank": mean(run["reciprocal_rank"] for run in runs),
            "mean_latency_ms": mean(latencies),
            "p50_latency_ms": latencies[(len(latencies) - 1) // 2],
            "p95_latency_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
            "mean_context_characters": mean(run["context_characters"] for run in runs),
            "max_context_characters": max(run["context_characters"] for run in runs),
            "mean_source_text_characters": mean(run["source_text_characters"] for run in runs),
            "contexts_over_24000": sum(run["context_characters"] > 24000 for run in runs),
            "query_measurements": len(runs)}


def benchmark(retriever: Retriever, cases: dict, *, k: int = 5,
              method: str = DEFAULT_METHOD) -> dict:
    """Measure reviewed-source recall, search latency and serialized context size.

    Judgments must exist before search. Latency includes query embedding and, on
    the first hybrid query, corpus validation/build; no answer model is called.
    """
    if method not in METHODS:
        raise ValueError(f"Unknown retrieval method: {method}")
    if type(k) is not int or k <= 0:
        raise ValueError("k must be a positive integer")
    if not cases["queries"]:
        raise ValueError("Benchmark must contain at least one query")
    # Resolve all reviewed sources before making any provider request.
    for case in cases["queries"]:
        if not case["query"].strip() or not case["relevant_sources"]:
            raise ValueError("Each query must have nonempty text and reviewed sources")
        for source in case["relevant_sources"]:
            if not retriever.get(source["id"], version=cases["version"], kind=case.get("kind")):
                raise ValueError(f"Reviewed source is missing or excluded by filter: {source['id']}")
    runs = []
    for case in cases["queries"]:
        started = perf_counter()
        results = retriever.search(case["query"], k=k, kind=case.get("kind"),
                                   version=cases["version"], method=method)
        elapsed_ms = (perf_counter() - started) * 1000
        serialized = [result.to_dict() for result in results]
        expected = {source["id"] for source in case["relevant_sources"]}
        found = expected.intersection(result.id for result in results)
        relevant_ranks = [rank for rank, result in enumerate(results, 1) if result.id in expected]
        runs.append({**case, "results": serialized,
                     "latency_ms": elapsed_ms,
                     "reciprocal_rank": 1 / min(relevant_ranks) if relevant_ranks else 0,
                     "context_characters": len(json.dumps(serialized, ensure_ascii=False)),
                     "source_text_characters": sum(len(result.title) + len(result.text) for result in results),
                     "search": retriever.last_search,
                     "matched_source_ids": sorted(found),
                     "reviewed_source_recall_at_k": len(found) / len(expected)})
    return {"method": method, "candidate_limit": retriever.candidate_limit,
            "rrf_constant": RRF_CONSTANT if method == "hybrid" else None,
            "case_review": cases.get("review"), "decision_policy": cases.get("decision_policy"),
            "summary": summarize(runs),
            "created_at": datetime.now(timezone.utc).isoformat(), "k": k,
            "version": cases["version"], "collection": retriever.collection.name,
            "collection_count": retriever.collection.count(), "configuration": retriever.contract,
            "distance_explanation": DISTANCE_EXPLANATION,
            "interpretation": "Reviewed sources are a non-exhaustive relevance set; missing sources are observations, not test failures.",
            "queries": runs}


def sweep(retriever: Retriever, cases: dict, *, repeats: int = 3) -> dict:
    """Run a fixed 16-configuration matrix with fresh embeddings for every search.

    Shuffle configuration order with a saved seed each round to reduce ordering
    bias. Keep full sources/candidates for the first round and compact repeated
    measurements thereafter. Lexical setup is timed separately, outside searches.
    """
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    configurations = [(method, k, limit) for k in (5, 8, 10, 20)
                      for method, limit in [("vector", 20), ("hybrid", 20),
                                            ("hybrid", 40), ("hybrid", 80)]]
    started = perf_counter()
    retriever._lexical_index(cases["version"])
    setup_ms = (perf_counter() - started) * 1000
    randomizer = random.Random(42)
    measurements = {}
    schedule = []
    for repetition in range(repeats):
        order = list(configurations)
        randomizer.shuffle(order)
        for method, k, limit in order:
            key = f"{method}-k{k}-c{limit}" if method == "hybrid" else f"vector-k{k}"
            schedule.append({"repetition": repetition + 1, "configuration": key})
            retriever.candidate_limit = limit
            report = benchmark(retriever, cases, k=k, method=method)
            if key not in measurements:
                measurements[key] = {"first_run": report, "samples": []}
            for run in report["queries"]:
                sample = {field: run[field] for field in (
                    "query", "latency_ms", "context_characters", "source_text_characters",
                    "matched_source_ids", "reviewed_source_recall_at_k", "reciprocal_rank")}
                sample.update(repetition=repetition + 1,
                              result_ids=[result["id"] for result in run["results"]])
                measurements[key]["samples"].append(sample)
    for configuration in measurements.values():
        configuration["summary"] = summarize(configuration["samples"])
        first_ids = {run["query"]: [result["id"] for result in run["results"]]
                     for run in configuration["first_run"]["queries"]}
        configuration["summary"]["rankings_changed_from_first"] = sum(
            sample["result_ids"] != first_ids[sample["query"]]
            for sample in configuration["samples"])
    return {"created_at": datetime.now(timezone.utc).isoformat(), "repeats": repeats,
            "configuration_count": len(configurations), "random_seed": 42,
            "lexical_setup_ms": setup_ms, "schedule": schedule,
            "measurement": "Fresh provider embeddings on every search; no query cache. "
                           "Search wall time includes network. JSONL validation and lexical setup are separate. "
                           "Full sources/candidates are retained for round one; all rounds retain IDs and metrics. "
                           "Contexts are raw retrieval arrays, without workflow filtering, expansion or truncation. "
                           "The 24000-character count is diagnostic, not a workflow budget simulation. "
                           "No generated answers were evaluated. Judgments are unchanged from the initial comparison.",
            "configurations": measurements}


def main() -> None:
    """Save one method or a paired comparison using the same reviewed cases."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("data/retrieval/queries.json"))
    parser.add_argument("--output", type=Path, required=True, help="Destination snapshot; existing file is replaced")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--method", choices=(*METHODS, "both"), default=DEFAULT_METHOD)
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATES)
    parser.add_argument("--sweep", action="store_true", help="Compare k=5,8,10,20 and hybrid candidates=20,40,80")
    parser.add_argument("--repeats", type=int, default=3, help="Rounds per sweep configuration (default: 3)")
    args = parser.parse_args()
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        retriever = Retriever(args.db, args.collection, source_path=args.source,
                              candidate_limit=args.candidate_limit)
        if args.sweep:
            report = sweep(retriever, cases, repeats=args.repeats)
        elif args.method == "both":
            reports = {method: benchmark(retriever, cases, k=args.k, method=method) for method in METHODS}
            changes = []
            for vector, hybrid in zip(reports["vector"]["queries"], reports["hybrid"]["queries"]):
                vector_ids = [result["id"] for result in vector["results"]]
                hybrid_ids = [result["id"] for result in hybrid["results"]]
                changes.append({"query": vector["query"], "vector_ids": vector_ids,
                                "hybrid_ids": hybrid_ids,
                                "added_ids": sorted(set(hybrid_ids) - set(vector_ids)),
                                "removed_ids": sorted(set(vector_ids) - set(hybrid_ids)),
                                "recall_delta": hybrid["reviewed_source_recall_at_k"]
                                                - vector["reviewed_source_recall_at_k"]})
            report = {"runs": reports, "source_changes": changes,
                      "measurement": "Sequential vector then hybrid; wall time includes provider/network jitter. "
                                     "First hybrid query includes in-memory index setup. Characters are not tokens. "
                                     "This evaluates retrieval, not generated answer quality."}
        else:
            report = benchmark(retriever, cases, k=args.k, method=args.method)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OpenAIError as exc:
        parser.exit(1, f"Embedding request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, KeyError, TypeError, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Benchmark failed: {exc}\n")
    print(f"Saved {len(cases['queries'])} queries to {args.output}")


if __name__ == "__main__":
    main()
