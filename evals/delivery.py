"""Evaluate the delivered workflow with live APIs and isolated persistent memory.

Run manually via python -m evals.delivery. Use shared measurement wrappers;
write evidence after each case. This is a paid quality experiment, not a test.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from backend.generation.llm import LLM
from backend.retrieval.retrieval import Retriever
from backend.workflow.cli import _serialize
from backend.workflow.graph import QuestionWorkflow, initial_state
from evals.measurement import (
    MeasuredModel, MeasuredRetriever, UsageRecorder, distribution, snapshot, validate_cases,
)


def run_case(case, retriever, version):
    """Keep setup/follow-up in one DB, reopen the graph and retain all call costs."""
    usage = UsageRecorder()
    llm = LLM()
    llm.client.callbacks = [usage]
    model = MeasuredModel(llm, None, usage)
    measured = MeasuredRetriever(retriever, None)
    started = perf_counter()
    result = {"case_id": case["id"], "question": case["question"], "status": "error", "turns": []}
    try:
        with TemporaryDirectory() as directory:
            for question in [*case.get("setup", []), case["question"]]:
                state = initial_state(question, llm)
                nodes = []
                # A new instance reopens SQLite, as a restarted process would.
                workflow = QuestionWorkflow(measured, llm=model, version=version,
                                            memory_path=Path(directory) / "memory.sqlite")
                turn_started = perf_counter()
                for namespace, update in workflow.stream(question, conversation_id=case["id"]):
                    if not namespace:
                        for node, values in update.items():
                            nodes.append(node)
                            state.update(values)
                result["turns"].append({"question": question, "nodes": nodes,
                    "elapsed_ms": (perf_counter() - turn_started) * 1000,
                    "result": snapshot(state, model, measured, started),
                    "reviews": state["reviews"], "prompts": state["prompts"],
                    "card_prompts": state["card_prompts"], "exchanges": state["exchanges"],
                    "cards": state["cards"], "decision": state["decision"],
                    "trace": vars(workflow.trace_report)})
        result["status"] = "ok"
    except Exception as error:
        # Provider exception bodies can contain sensitive payloads; retain type only.
        result["error_type"] = type(error).__name__
    result.update(elapsed_ms=(perf_counter() - started) * 1000,
                  model_calls=model.calls, retrieval_calls=measured.calls)
    return json.loads(json.dumps(result, default=_serialize, ensure_ascii=False))


def summarize_report(report):
    """Measure costs/latencies and citation provenance, never infer semantic quality.

    Per-case measurements include setup turns. Token counts exclude embedding
    requests and unreported usage; monetary cost remains unknown.
    """
    rows = []
    for sample in report['samples']:
        turns = sample['turns']
        final = turns[-1]['result'] if turns and sample['status'] == 'ok' else None
        usage = [entry for call in sample['model_calls'] for entry in call['usage']]
        provenance = None
        if final and final['citations']:
            sources = {source['id']: source for source in final['evidence']['rules']}
            provenance = all(
                citation['source_id'] in sources and all(
                    citation[key] == sources[citation['source_id']][source_key]
                    for key, source_key in [('excerpt', 'text'), ('version', 'version'),
                                            ('page_start', 'page_start'), ('page_end', 'page_end')])
                for citation in final['citations'])
        rows.append(dict(case_id=sample['case_id'], status=sample['status'],
                         elapsed_ms=sample['elapsed_ms'], model_calls=len(sample['model_calls']),
                         calls_with_usage=sum(bool(call['usage']) for call in sample['model_calls']),
                         input_tokens=sum(item['input_tokens'] for item in usage),
                         output_tokens=sum(item['output_tokens'] for item in usage),
                         citation_provenance_valid=provenance,
                         sources=[c['source_id'] for c in final['citations']] if final else []))
    return dict(cases=rows, completed=sum(row['status'] == 'ok' for row in rows),
                elapsed_ms=distribution([row['elapsed_ms'] for row in rows]),
                input_tokens=sum(row['input_tokens'] for row in rows),
                output_tokens=sum(row['output_tokens'] for row in rows),
                model_calls=sum(row['model_calls'] for row in rows),
                calls_with_usage=sum(row['calls_with_usage'] for row in rows),
                semantic_quality='Requires separate judgments; production approval is not a score')


def main():
    """Validate reference IDs before spending tokens; checkpoint without overwrite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path('data/delivery/cases.json'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; use a new path to preserve prior results')
    raw = args.cases.read_bytes()
    bank = json.loads(raw)
    retriever = Retriever()
    validate_cases(bank, retriever)
    report = dict(created_at=datetime.now(timezone.utc).isoformat(), bank=bank,
                  cases_sha256=hashlib.sha256(raw).hexdigest(), model=vars(LLM().config),
                  index_contract=retriever.contract, collection_count=retriever.collection.count(),
                  settings={'workers': args.workers, 'workflow_defaults': True,
                            'tokens_include_setup': True, 'embedding_tokens_measured': False,
                            'cost_currency': None, 'repetitions': 1}, samples=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        tasks = [executor.submit(run_case, case, retriever, bank['version']) for case in bank['cases']]
        for task in as_completed(tasks):
            sample = task.result()
            report['samples'].append(sample)
            report['summary'] = summarize_report(report)
            temporary = args.output.with_suffix('.partial')
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
            temporary.replace(args.output)
            print(sample['case_id'], sample['status'], flush=True)


if __name__ == '__main__':
    main()
