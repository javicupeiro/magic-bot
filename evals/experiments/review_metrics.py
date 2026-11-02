"""Aggregate saved answer-review experiments without model calls or file writes.

The runner supplies paired snapshots. Optional external judgments assess factual
quality independently of the production judge; its approval is never a score.
"""

from collections import Counter
from statistics import mean
from evals.measurement import distribution

VARIANTS = ("baseline", "review_only", "final")
LABELS = {"correct", "incorrect", "incomplete", "abstained", "clarification"}


def summarize(samples, judgments=None):
    """Keep natural cases and controlled probes separate, including failed runs.

    A wrong answer becoming an abstention counts as safer rejection, not a newly
    correct rules answer. Quality remains null until independent labels exist.
    """
    judgments = judgments or {}
    groups = {}
    for cohort in sorted({sample["cohort"] for sample in samples}, key=lambda name: name == "controlled"):
        all_runs = [sample for sample in samples if sample["cohort"] == cohort]
        runs = [sample for sample in all_runs if sample["status"] == "ok"]
        reviewed = [run for run in runs if run["reviews"]]
        variants = {}
        for variant in VARIANTS:
            snapshots = [run["variants"][variant] for run in runs]
            variants[variant] = {
                "model_calls": distribution([s["model_calls"] for s in snapshots]),
                "search_calls": distribution([s["search_calls"] for s in snapshots]),
                "real_search_calls": distribution([s["real_search_calls"] for s in snapshots]),
                "elapsed_ms": distribution([s["elapsed_ms"] for s in snapshots]),
                "input_tokens_reported": sum(s["input_tokens"] for s in snapshots),
                "output_tokens_reported": sum(s["output_tokens"] for s in snapshots),
                "calls_with_usage": sum(s["calls_with_usage"] for s in snapshots),
                "total_model_calls": sum(s["model_calls"] for s in snapshots),
                "answer_rate": mean(s["can_answer"] for s in snapshots) if snapshots else None,
            }
        labeled = []
        for run in runs:
            labels = judgments.get(run["sample_id"])
            if labels is None:
                continue
            if set(labels) != {*VARIANTS, "note"} or not labels["note"].strip():
                raise ValueError(f"Judgment needs all variants and an evidence note: {run['sample_id']}")
            if any(labels[v] not in LABELS for v in VARIANTS):
                raise ValueError(f"Unknown quality label: {run['sample_id']}")
            labeled.append((run, labels))
        quality = None
        if labeled:
            def success(run, label):
                """Distinguish corpus-answerable cases from required stop conditions."""
                expected = run["expectation"]
                return label == {"answer": "correct", "abstain": "abstained",
                                 "clarify": "clarification"}[expected]
            quality = {"labeled_pairs": len(labeled), "completed_pairs": len(runs),
                       "labels": {v: dict(Counter(labels[v] for _, labels in labeled)) for v in VARIANTS},
                       "task_success_rate": {
                           v: mean(success(run, labels[v]) for run, labels in labeled) for v in VARIANTS},
                       "corrected_answers": sum(labels["baseline"] in {"incorrect", "incomplete", "abstained"}
                                                 and labels["final"] == "correct" for _, labels in labeled),
                       "lost_correct_answers": sum(labels["baseline"] == "correct" and labels["final"] != "correct"
                                                   for _, labels in labeled),
                       "safer_rejections": sum(labels["baseline"] in {"incorrect", "incomplete"}
                                               and labels["final"] == "abstained" for _, labels in labeled),
                       "introduced_wrong_answers": sum(labels["baseline"] != "incorrect" and labels["final"] == "incorrect"
                                                       for _, labels in labeled),
                       "false_rejections": sum(labels["baseline"] == "correct" and labels["review_only"] == "abstained"
                                               for _, labels in labeled)}
        groups[cohort] = {
            "scheduled": len(all_runs), "completed": len(runs), "failed": len(all_runs) - len(runs),
            "reviewed": len(reviewed), "repaired": sum(run["additional_retrievals"] == 1 for run in runs),
            "retry_rate_among_reviewed": (mean(run["additional_retrievals"] == 1 for run in reviewed)
                                          if reviewed else None),
            "round_limit_violations": sum(run["additional_retrievals"] > 1 for run in runs),
            "query_limit_violations": sum(len(run["executed_repair_queries"]) > 3 for run in runs),
            "context_budget_violations": sum(run["context_budget_exceeded"] for run in runs),
            "first_review_approvals": sum(run["reviews"][0]["approved"] for run in reviewed),
            "final_review_approvals": sum(run["reviews"][-1]["approved"] for run in reviewed),
            "extra_model_calls": distribution([run["variants"]["final"]["model_calls"]
                                                 - run["variants"]["baseline"]["model_calls"] for run in runs]),
            "extra_search_calls": distribution([run["variants"]["final"]["search_calls"]
                                                  - run["variants"]["baseline"]["search_calls"] for run in runs]),
            "model_calls_by_operation": dict(Counter(call["operation"] for run in runs for call in run["model_calls"])),
            "probes_applied": sum(run["probe_applied"] for run in runs),
            "changed_answers": sum(run["variants"]["baseline"]["answer"] != run["variants"]["final"]["answer"]
                                   for run in runs),
            "variants": variants, "quality": quality,
        }
    unknown = judgments.keys() - {sample["sample_id"] for sample in samples}
    if unknown:
        raise ValueError(f"Judgments reference unknown sample IDs: {sorted(unknown)}")
    return groups
