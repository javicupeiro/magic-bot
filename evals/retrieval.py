"""Save retrieval results against a small set of manually reviewed sources."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import chromadb
from openai import OpenAIError

from backend.retrieval.retrieval import DISTANCE_EXPLANATION, Retriever


def benchmark(retriever: Retriever, cases: dict, *, k: int = 5) -> dict:
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
        results = retriever.search(case["query"], k=k, kind=case.get("kind"), version=cases["version"])
        expected = {source["id"] for source in case["relevant_sources"]}
        found = expected.intersection(result.id for result in results)
        runs.append({**case, "results": [result.to_dict() for result in results],
                     "matched_source_ids": sorted(found),
                     "reviewed_source_recall_at_k": len(found) / len(expected)})
    return {"created_at": datetime.now(timezone.utc).isoformat(), "k": k,
            "version": cases["version"], "collection": retriever.collection.name,
            "collection_count": retriever.collection.count(), "configuration": retriever.contract,
            "distance_explanation": DISTANCE_EXPLANATION,
            "interpretation": "Reviewed sources are a non-exhaustive relevance set; missing sources are observations, not test failures.",
            "queries": runs}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("data/retrieval/queries.json"))
    parser.add_argument("--output", type=Path, required=True, help="Destination snapshot; existing file is replaced")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        report = benchmark(Retriever(args.db, args.collection), cases, k=args.k)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OpenAIError as exc:
        parser.exit(1, f"Embedding request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, KeyError, TypeError, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Benchmark failed: {exc}\n")
    print(f"Saved {len(report['queries'])} queries to {args.output}")


if __name__ == "__main__":
    main()
