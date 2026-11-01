"""Run catalogue lookups from the terminal and print their results as JSON.

This module translates terminal arguments into calls to MtgApiClient. It lets us
inspect the provider without starting the chatbot or using a language model.
Other Python modules can import MtgApiClient directly instead of using this CLI.

Run from the repository root (global options go before the subcommand)::

    uv run --locked card-catalogue --max-pages 6 search --color White --subtype Warrior --cmc '<2'
    uv run --locked card-catalogue name 'Vanguardia de Adanto' --language Spanish
    uv run --locked card-catalogue sets --name Khans --released-from 2014-01-01

Quote '<2' and '<=2' so the shell does not interpret '<' as input redirection.
The module entry point also works: uv run --locked python -m backend.cards.cli --help
"""

import argparse
import json

from backend.cards.mtg_api import MtgApiClient


def main() -> None:
    """Parse a command, execute one lookup, print JSON and signal its outcome.

    Exit codes are 0 for complete results (including empty), 1 for API failures,
    and 2 for partial results, ambiguous names or invalid command arguments.
    """
    # These options configure HTTP access and apply to all three commands.
    # Preserve example line breaks when displaying --help.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--retries", type=int, default=1)
    # Each subcommand has its own arguments: broad search, exact name, or sets.
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("--name")
    search.add_argument("--language")
    search.add_argument("--subtype")
    search.add_argument("--color")
    search.add_argument("--color-identity")
    search.add_argument("--cmc", help="Mana value: 0, <2, <=2 or =2")
    resolve = commands.add_parser("name")
    resolve.add_argument("name")
    resolve.add_argument("--language")
    sets = commands.add_parser("sets")
    sets.add_argument("--name")
    sets.add_argument("--released-from")
    sets.add_argument("--released-to")
    # argparse turns --color-identity into color_identity, matching the API method.
    # Separate shared client settings from arguments passed to the chosen method.
    args = vars(parser.parse_args())
    command = args.pop("command")
    config = {key: args.pop(key) for key in ("max_pages", "page_size", "timeout", "retries")}
    try:
        # The context manager closes the HTTP connection pool, even on failure.
        with MtgApiClient(**config) as api:
            method = {"search": api.search_cards, "name": api.get_card_by_name, "sets": api.list_sets}[command]
            result = method(**args)
    except ValueError as exc:
        # Invalid filters are usage errors; provider failures are returned in result.
        parser.error(str(exc))
    # Print metadata as well as cards: an empty partial lookup cannot prove absence.
    # Keep Unicode intact so localized names remain readable in the terminal.
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    # An error takes precedence when a failed later page also makes results partial.
    if result.error:
        raise SystemExit(1)
    if result.partial or result.status == "ambiguous":
        raise SystemExit(2)


if __name__ == "__main__":
    # Execute only when launched as a module; importing this file sends no requests.
    main()
