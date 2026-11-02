"""Ask card questions with readable output and optional diagnostic details.

Examples:
uv run --locked card-chat 'Find white Warriors with mana value <2'
uv run --locked card-chat 'Find Lightning Bolt and show its mana cost and card text.'

Use --trace for compact tool calls or --json for the complete structured record.
"""

import argparse
import json

from openai import OpenAIError

from backend.cards.agent import CardAgent
from backend.cards.replies import CardAgentResult
from backend.cards.mtg_api import MtgApiClient
from backend.common.tracing import warn_missing_trace


def format_reply(result: CardAgentResult, *, trace: bool = False) -> str:
    """Show prose and a small lookup summary without dumping card printings.

    Surface incomplete or failed lookups even when the model omits them in its
    prose. Diagnostic mode adds one request/result line per tool, never records.
    """
    spanish = result.reply.language == "es"
    pages = sum(exchange.result.get("pages_fetched", 0) for exchange in result.exchanges)
    summary = (f"Consultas: {len(result.exchanges)} · Cartas: {len(result.cards)} · Páginas: {pages}"
               if spanish else
               f"Lookups: {len(result.exchanges)} · Cards: {len(result.cards)} · Pages: {pages}")
    lines = [result.reply.text.strip(), "", summary]
    statuses = ({"partial": "resultado incompleto", "error": "fallo del catálogo",
                 "ambiguous": "nombre ambiguo", "rejected": "solicitud rechazada",
                 "empty": "sin coincidencias", "ok": "correcto"} if spanish else
                {"partial": "incomplete result", "error": "catalogue failure",
                 "ambiguous": "ambiguous name", "rejected": "request rejected",
                 "empty": "no matches", "ok": "ok"})
    for index, exchange in enumerate(result.exchanges, start=1):
        data = exchange.result
        status = statuses.get(data["status"], data["status"])
        if trace:
            # Only validated JSON objects get prettified; retain malformed requests.
            try:
                arguments = json.loads(exchange.arguments)
                if isinstance(arguments, dict):
                    arguments = {key: value for key, value in arguments.items() if value is not None}
                arguments = json.dumps(arguments, ensure_ascii=False)
            except ValueError:
                arguments = exchange.arguments
            lines.append(f"  {index}. {exchange.name} {arguments} → {status} [{exchange.call_id}]")
        elif data["status"] in {"partial", "error", "ambiguous", "rejected"}:
            lines.append(f"  {index}. {exchange.name}: {status}")
        if data.get("error"):
            error = data["error"]
            detail = error.get("message") or error["kind"]
            lines.append(f"     {detail}")
    return "\n".join(lines)


def main() -> None:
    """Run a bounded conversation and select readable or full JSON output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="English or Spanish card question")
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--retries", type=int, default=1)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print the full structured record")
    output.add_argument("--trace", action="store_true", help="Show compact tool arguments, status and call IDs")
    args = parser.parse_args()
    try:
        with MtgApiClient(max_pages=args.max_pages, page_size=args.page_size,
                          timeout=args.timeout, retries=args.retries) as api:
            agent = CardAgent(api, max_calls=args.max_calls)
            result = agent.answer(args.question)
            warn_missing_trace(agent.trace_report)
    except OpenAIError as exc:
        parser.exit(1, f"Model request failed ({type(exc).__name__})\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Card conversation failed: {exc}\n")
    print(result.model_dump_json(indent=2) if args.json else format_reply(result, trace=args.trace))


if __name__ == "__main__":
    main()
