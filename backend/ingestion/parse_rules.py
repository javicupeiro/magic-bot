"""Parse page-separated Comprehensive Rules text into structured JSONL."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from backend.ingestion.documents import Document, Reference, document_id
from backend.ingestion.pdf_to_text import PAGE_SEPARATOR

DEFAULT_INPUT = Path("data/processed/MagicCompRules_20260417.txt")
RULE = re.compile(r"^(\d{3})(?:\.(\d+)(?:([a-z])\.?|\.)|\.)(\s*[A-Z∞“({].*)$")
SECTION = re.compile(r"^([1-9])\.\s+(.+)$")
NUMBER = r"\d{3}(?:\.\d+[a-z]?)?"
REF_GROUP = re.compile(
    rf"\b(rules?|sections?)\s+({NUMBER}|[1-9])(?!\d)", re.IGNORECASE
)
REF_NEXT = re.compile(rf"\s*(?:,\s*(?:and\s+|or\s+)?|and\s+|or\s+)({NUMBER}|[1-9])(?!\d)")
REF_RANGE = re.compile(rf"\s*[–—-]\s*({NUMBER}|\d+|[a-z])(?![\w])")
SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with", "d20"}


def is_term(line: str) -> bool:
    """The source's glossary headings use title case and no sentence punctuation.

    Keep this format-specific heuristic explicit: arbitrary prose or differently
    formatted glossaries need a separate parser, not token-size chunking.
    """
    if not line or len(line) > 100 or re.search(r'[.{}:;\d]', line.replace('d20', '')):
        return False
    if line.count('“') != line.count('”') or line.count('(') != line.count(')'):
        return False
    words = line.split()
    return all(
        word.strip('(),[]“”"')[0:1].isupper() or word in SMALL_WORDS
        for word in words
    ) and line[0].isupper()


def extract_references(text: str, version: str) -> list[Reference]:
    """Extract explicit rule/section lists and ranges, plus glossary 'See' links."""
    found = {}

    def add(kind, identifier):
        key = (kind, identifier)
        found[key] = Reference(kind, identifier, document_id(version, kind, identifier))

    for match in REF_GROUP.finditer(text):
        kind = "section" if match[1].lower().startswith("section") else "rule"
        identifier = match[2]
        add(kind, identifier)
        pos = match.end()
        while True:
            span = REF_RANGE.match(text, pos)
            if span:
                end = span[1]
                if re.fullmatch(r"[a-z]", end) and identifier[-1].isalpha():
                    end = identifier[:-1] + end
                elif end.isdigit() and len(end) < 3 and '.' in identifier:
                    end = identifier.rsplit('.', 1)[0] + '.' + end
                # Rulebook subrules deliberately skip l and o.
                if identifier[:-1] == end[:-1] and identifier[-1].isalpha() and end[-1].isalpha():
                    for letter in range(ord(identifier[-1]) + 1, ord(end[-1]) + 1):
                        if chr(letter) not in "lo":
                            add(kind, identifier[:-1] + chr(letter))
                elif identifier.isdigit() and end.isdigit():
                    for number in range(int(identifier) + 1, int(end) + 1):
                        add(kind, str(number))
                elif re.fullmatch(r"\d{3}\.\d+", identifier) and re.fullmatch(r"\d{3}\.\d+", end) and identifier.split('.')[0] == end.split('.')[0]:
                    prefix, first = identifier.split('.')
                    for number in range(int(first) + 1, int(end.split('.')[1]) + 1):
                        add(kind, f"{prefix}.{number}")
                else:
                    add(kind, end)
                pos = span.end()
                identifier = end
                continue
            following = REF_NEXT.match(text, pos)
            if not following:
                break
            identifier = following[1]
            add(kind, identifier)
            pos = following.end()
    for match in re.finditer(r"\bSee (?:also )?([^.!?]+)[.]", text):
        term = match[1].strip()
        if is_term(term):
            add("glossary", term)
    return list(found.values())


def parse_text(text: str, version: str) -> list[Document]:
    """Preserve source lines and physical (one-based) page spans per fragment."""
    if not version.strip():
        raise ValueError("Edition/version must not be empty")
    pages = text.split(PAGE_SEPARATOR)
    lines = [(p, line.strip()) for p, page in enumerate(pages, 1) for line in page.splitlines()]
    # Start at the section heading immediately before the first actual subrule.
    # This excludes introduction and multi-page contents without fixed pages.
    first_rule = next((i for i, (_, line) in enumerate(lines) if re.match(r"^\d{3}\.\d+\.\s*", line)), None)
    if first_rule is None:
        raise ValueError("No numbered rules found")
    start = first_rule
    for i in range(first_rule - 1, -1, -1):
        if SECTION.match(lines[i][1]):
            start = i
            break
    documents = []
    current = None
    body = []
    glossary = False
    section_titles = set()

    def finish():
        if current is not None:
            current.text = "\n".join(body).strip()
            current.references = extract_references(" ".join(body), version)
            documents.append(current)

    for line_index in range(start, len(lines)):
        page, line = lines[line_index]
        if not line or line.isdigit() or line == "Magic: The Gathering Comprehensive Rules" or line.startswith("These rules are effective as of"):
            continue
        if line == "Credits":
            finish()
            current = None
            break
        if line == "Glossary":
            if not glossary:
                finish()
                current, body = None, []
                glossary = True
            continue
        kind = identifier = title = parent = None
        section = SECTION.match(line) if not glossary else None
        # Numbered paragraphs in examples are prose, not major sections.
        if section and not any(
            re.match(r"^\d{3}\.\s+", following)
            for _, following in lines[line_index + 1:line_index + 3]
        ):
            section = None
        if line in section_titles:
            continue
        rule = RULE.match(line) if not glossary else None
        if rule and current and current.rule_number and '.' in current.rule_number and body and re.search(r"\b(?:rules?|sections?)\s*$", body[-1], re.IGNORECASE):
            rule = None
        if rule and rule[2] is None:
            following = next((value for _, value in lines[line_index + 1:] if value), "")
            if not re.match(rf"^{rule[1]}\.\d+\.", following):
                rule = None
        if glossary and is_term(line):
            kind, identifier, title = "glossary", line, line
        elif section:
            kind, identifier, title = "section", section[1], section[2]
            section_titles.add(line)
        elif rule:
            kind = "rule"
            identifier = rule[1] + (f".{rule[2]}{rule[3] or ''}" if rule[2] else "")
            title = rule[4].strip()
            parent_number = f"{rule[1]}.{rule[2]}" if rule[3] else rule[1]
            parent = document_id(version, "rule", parent_number) if rule[2] else document_id(version, "section", rule[1][0])
        if kind:
            finish()
            current = Document(
                id=document_id(version, kind, identifier), kind=kind, text="", version=version,
                page_start=page, page_end=page, title=title, parent_id=parent,
                rule_number=identifier if kind == "rule" else None,
                term=identifier if kind == "glossary" else None,
                section_number=identifier if kind == "section" else None,
            )
            body = [title] if title and kind != "glossary" else []
        elif current is not None:
            body.append(line)
            current.page_end = page
    else:
        finish()
    return documents


def summarize(documents: list[Document]) -> dict:
    counts = Counter(document.id for document in documents)
    for document in documents:
        for reference in document.references:
            reference.resolved = counts[reference.target_id] == 1
    return {
        "total": len(documents),
        "counts_by_kind": dict(Counter(document.kind for document in documents)),
        "duplicate_ids": {key: count for key, count in counts.items() if count > 1},
        "empty_fragments": [document.id for document in documents if not document.text.strip()],
        "unresolved_references": [
            {"source_id": document.id, **reference.__dict__}
            for document in documents for reference in document.references if not reference.resolved
        ],
        "unresolved_parents": [
            {"source_id": document.id, "parent_id": document.parent_id}
            for document in documents if document.parent_id and counts[document.parent_id] != 1
        ],
    }


def parse_rules(input_path: Path, output_path: Path, report_path: Path, version: str | None = None) -> dict:
    if len({path.resolve() for path in (input_path, output_path, report_path)}) != 3:
        raise ValueError("Input, JSONL and report must be different paths")
    text = input_path.read_text(encoding="utf-8")
    if version is None:
        match = re.search(r"These rules are effective as of\s+([^\n]+)", text)
        if not match:
            raise ValueError("Declared edition not found; supply --version")
        version = match[1].strip().rstrip(".")
    documents = parse_text(text, version)
    report = {"source_text": str(input_path), "version": version, **summarize(documents)}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["duplicate_ids"] or report["empty_fragments"]:
        raise ValueError(f"Duplicate IDs or empty fragments detected; review {report_path}. JSONL was not written")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(document.to_dict(), ensure_ascii=False) + "\n" for document in documents), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--version", help="Override the edition declared in the source")
    parser.add_argument("--id", help="Print one generated document by its exact ID")
    args = parser.parse_args()
    output = args.output or args.input.with_suffix(".jsonl")
    report_path = args.report or output.with_suffix(".report.json")
    try:
        report = parse_rules(args.input, output, report_path, args.version)
        if args.id:
            for line in output.read_text(encoding="utf-8").splitlines():
                document = json.loads(line)
                if document["id"] == args.id:
                    print(json.dumps(document, ensure_ascii=False, indent=2))
                    return
            raise ValueError(f"Unknown document ID: {args.id}")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Parsing failed: {exc}\n")
    print(f"Parsed {report['total']} documents: {report['counts_by_kind']}")
    print(f"JSONL: {output}\nReport: {report_path}")
    if report["unresolved_references"] or report["unresolved_parents"]:
        print(f"Warning: {len(report['unresolved_references'])} unresolved references and {len(report['unresolved_parents'])} unresolved parents; review {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
