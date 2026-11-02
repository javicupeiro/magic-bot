"""Run a Magic question and inspect the workflow from the command line.

Examples (run from the project root after ``uv sync --locked``)::

    # A definition: normalize, retrieve rules, generate and validate citations.
    uv run --locked magic-chat 'What is first strike?'

    # A card search: execute model-selected catalogue tools without a rules index.
    uv run --locked magic-chat 'Find Lightning Bolt and show its mana cost.'

    # An interaction: retrieve evidence for timing and combat-damage eligibility.
    uv run --locked magic-chat 'Can I use ninjutsu after first-strike damage?'

    # Spanish questions keep Spanish answers; retrieval queries use English.
    uv run --locked magic-chat '¿Qué es dañar primero?'

    # Save the full state, including cards, tool call IDs, queries and sources.
    uv run --locked magic-chat 'Find Lightning Bolt' --json > tmp/card-search.json

    # Adjust retrieval and execution limits independently.
    uv run --locked magic-chat 'What is first strike?' -k 3 --context-budget 12000
    uv run --locked magic-chat 'Find Lightning Bolt' --max-calls 2 --max-pages 1

    # The module entry point behaves like the installed command.
    uv run --locked python -m backend.workflow.cli 'What is first strike?'

Real runs use the existing OpenAI environment configuration. Rules questions also
need the persisted Chroma index (data/chroma by default); card searches need the
external catalogue. The index opens only if a node actually requests rules.

Readable output starts with the answer, followed by node summaries, original
tool exchanges and citations. --json instead prints the complete workflow state
as one JSON object, suitable for saving or inspecting with another program.
"""

import argparse
import json
from pathlib import Path
import sqlite3

import chromadb
from openai import OpenAIError
from pydantic import BaseModel

from backend.cards.mtg_api import MtgApiClient
from backend.common.tracing import warn_missing_trace
from backend.ingestion.ingest import DEFAULT_INPUT
from backend.retrieval.hybrid import DEFAULT_CANDIDATES
from backend.retrieval.retrieval import DEFAULT_K, DEFAULT_METHOD, METHODS, Retriever, SearchResult
from backend.workflow.graph import QuestionWorkflow
from backend.workflow.memory import DEFAULT_MEMORY_PATH


def _serialize(value):
    """Teach json.dumps how to encode the non-JSON objects kept in graph state.

    Pydantic models include routing decisions, drafts, citations and LangChain
    messages. SearchResult is a dataclass with its own provenance serializer.
    Ordinary dictionaries/lists already work with JSON and never reach here.
    Unknown objects raise TypeError rather than silently becoming opaque text.
    """
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, SearchResult):
        return value.to_dict()
    raise TypeError(f"Unsupported result type: {type(value).__name__}")


def main():
    """Parse options, execute a new or resumed conversation turn, and render it.

    Exit codes: 0 for a completed run (including clarification or abstention),
    1 for handled model/index/configuration failures, and 2 for invalid CLI use.
    This function owns CLI concerns; graph routing belongs to QuestionWorkflow.
    """
    # 1. Describe the interface. Preserve example line breaks in --help so commands
    # can be copied directly; argparse otherwise collapses the module docstring.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", help="English or Spanish question; quote it as one argument")
    parser.add_argument("--conversation-id", help="Resume this conversation; omission starts a new one")
    parser.add_argument("--memory-db", type=Path, default=DEFAULT_MEMORY_PATH,
                        help="SQLite checkpoint file (default: data/conversations/checkpoints.sqlite)")
    parser.add_argument("--history-budget", type=int, default=8000,
                        help="Serialized recent history plus current question characters (default: 8000)")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"),
                        help="Persisted rules index directory (default: data/chroma)")
    parser.add_argument("--collection", default="magic-rules",
                        help="Existing Chroma collection (default: magic-rules)")
    parser.add_argument("--version", help="Restrict retrieval to an exact rulebook edition")
    parser.add_argument("-k", type=int, default=DEFAULT_K,
                        help=f"Results per query and kind, before deduplication (default: {DEFAULT_K})")
    parser.add_argument("--max-calls", type=int, default=4,
                        help="Total card tool requests, including rejected requests (default: 4)")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="Maximum HTTP pages per catalogue lookup (default: 3)")
    parser.add_argument("--context-budget", type=int, default=24000,
                        help="Serialized rule/card evidence characters, not tokens (default: 24000)")
    parser.add_argument("--json", action="store_true",
                        help="Print the entire structured state instead of readable output")
    parser.add_argument("--events", action="store_true", help="Stream native node updates, including card tools")
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD)
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT, help="Matching JSONL for BM25")
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATES)
    args = parser.parse_args()
    try:
        # 2. Prepare a retrieval proxy without opening Chroma yet. Routing happens
        # later: a card-only request should work even when no rules index exists.
        class LazyRetriever:
            """Create and reuse one real Retriever on the first rules operation.

            This CLI-local adapter captures the parsed database options. It offers
            the two operations required by workflow nodes: configured search and
            exact rule lookup. Creating the adapter itself performs no retrieval.
            """
            instance = None

            def _get(self):
                """Open the configured index once; let index errors reach main()."""
                if self.instance is None:
                    self.instance = Retriever(
                        args.db, args.collection, method=args.method,
                        source_path=args.source, candidate_limit=args.candidate_limit)
                return self.instance

            def search(self, *values, **options):
                """Forward the configured retrieval method, preserving query, kind and edition filters."""
                return self._get().search(*values, **options)

            def get(self, *values, **options):
                """Forward numbered-rule lookup used by one-hop reference expansion."""
                return self._get().get(*values, **options)

        # 3. Inject dependencies and limits, then execute the graph. The factory
        # creates the catalogue client when the workflow opens; its context manager
        # keeps the pool open for all nodes and releases owned connections on exit.
        # The model cannot choose these HTTP settings through tool arguments.
        workflow = QuestionWorkflow(LazyRetriever(), k=args.k, version=args.version,
            max_calls=args.max_calls, context_budget=args.context_budget,
            memory_path=args.memory_db, history_budget=args.history_budget,
            api_factory=lambda: MtgApiClient(max_pages=args.max_pages))
        conversation_id = args.conversation_id if args.conversation_id is not None else workflow.new_conversation()
        if args.events:
            print(json.dumps({"conversation_id": conversation_id}, ensure_ascii=False), flush=True)
            for namespace, update in workflow.stream(args.question, conversation_id=conversation_id):
                # Only publish node names here: raw messages may include private
                # provider metadata. The later HTTP adapter will allowlist fields.
                print(json.dumps({"namespace": namespace, "nodes": list(update)}, ensure_ascii=False), flush=True)
            warn_missing_trace(workflow.trace_report)
            return
        state = workflow.answer(args.question, conversation_id=conversation_id)
        warn_missing_trace(workflow.trace_report)
    except OpenAIError as exc:
        # Avoid printing provider exception bodies, which can contain request data.
        parser.exit(1, f"Model request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, sqlite3.Error, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Workflow failed: {exc}\n")

    # 4. Choose one output format. ensure_ascii=False preserves Spanish text;
    # default=_serialize handles the typed evidence objects inside the state.
    if args.json:
        print(json.dumps(state, default=_serialize, ensure_ascii=False, indent=2))
        return

    # The readable form leads with the answer, then exposes the execution trail.
    # Tool exchanges retain full results and IDs; citations retain source text.
    print(state["answer"])
    print("\nConversation: " + state["conversation_id"])
    print("\nNodes: " + " → ".join(item["node"] for item in state["trace"]))
    for item in state["trace"]:
        print(json.dumps(item, ensure_ascii=False))
    for exchange in state["exchanges"]:
        print(json.dumps(exchange, ensure_ascii=False))
    for citation in state["citations"]:
        print(citation.model_dump_json())


if __name__ == "__main__":
    # Support python -m backend.workflow.cli as well as the magic-chat script.
    main()
