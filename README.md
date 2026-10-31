# Magic bot

Local ingestion tools that extract the rulebook's existing text layer and parse rules and glossary entries into structured documents, without OCR or language models.

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it). Run from the repository root:

```sh
uv sync --locked
uv run --locked pdf-to-text
```

The only runtime dependency is `pypdf`. Hatchling packages the `backend` directory for installation. Document ingestion code lives in `backend/ingestion/`.

The command reads `data/game_rules/MagicCompRules_20260417.pdf`, preserves the original, and writes:

- [data/processed/MagicCompRules_20260417.txt](data/processed/MagicCompRules_20260417.txt): UTF-8 text with `\n\f\n` between pages. `\f` is the form feed character (U+000C). Splitting on this separator recovers pages in order, including empty pages. Rulebook TXT files are included in version control for use by the project.
- `data/processed/MagicCompRules_20260417.manifest.json`: PDF SHA-256, edition declared in the document, page count, extractor version, pypdf version, extraction mode, character counts per page, and warnings. Generated manifests remain ignored by Git.

Configurable paths:

```sh
uv run --locked pdf-to-text --input path/document.pdf --output data/processed/document.txt --manifest data/processed/document.manifest.json
```

You can also run `uv run --locked python -m backend.ingestion.pdf_to_text`. Without `--manifest`, the manifest uses the TXT filename with a `.manifest.json` extension. Default paths are relative to the current working directory. Outputs go in `data/processed/`, separate from the Python code. Git includes `MagicCompRules_*.txt` files in that directory; other generated outputs remain ignored.

In [pdf_to_text.py](backend/ingestion/pdf_to_text.py), `main()` resolves paths and `extract_pdf()` reads the bytes, computes the hash, extracts each page, and writes the text and manifest. It does not normalize whitespace or correct content. Empty pages, unusually short pages, and pages containing suspicious characters are preserved and reported through stderr and the manifest. These checks are heuristics and cannot detect every extraction problem. Scanned PDFs need a separate workflow; this command reports missing text without attempting OCR.

Parse the extracted text:

```sh
uv run --locked parse-rules
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:rule:100.1a'
uv run --locked parse-rules --id 'mtg:April%2017%2C%202026:glossary:Abandon'
uv run --locked python -m unittest discover -s tests -v
```

The parser writes `data/processed/MagicCompRules_20260417.jsonl` and a `.report.json` summary. Both are reproducible generated files ignored by Git. Customize them with `--input`, `--output`, and `--report`. The edition comes from the source's effective-date declaration; `--version` overrides it or supplies a missing edition. `python -m backend.ingestion.parse_rules` provides the same CLI. `--id` regenerates the files and prints the requested document, failing if the ID is unknown.

The shared contract in [documents.py](backend/ingestion/documents.py) has `id`, `kind`, `text`, `version`, one-based physical `page_start`/`page_end`, `title`, and `references`. Each rule or subrule has `rule_number` and `parent_id`; major sections have `kind: section` and `section_number`, so references such as “section 8” have a target. Glossary entries have `term` and a null `rule_number`. Inapplicable fields are null. Rule text excludes the identifying number; glossary text excludes the term heading. Examples and source line breaks remain in the body, including continuations across pages. For rules, the title is the text following the number on its first source line.

IDs follow `mtg:<edition>:<kind>:<identifier>`, with edition and identifier URL-encoded losslessly. They depend on the declared edition and rule number, section number, or exact glossary term, never page position. References contain `kind`, `identifier`, `target_id`, and `resolved`. Explicit rule/section lists and numeric or letter ranges are parsed deterministically, skipping the unused subrule letters l and o. Glossary “See” links are also extracted; exact terms resolve automatically, while aliases and ambiguous lists remain in the review report.

The report includes counts by kind, duplicate IDs, empty fragments, unresolved references, and unresolved parents. Duplicate IDs or empty fragments cause a nonzero exit after writing the report, leaving any existing JSONL untouched. Unresolved references produce a warning for review. No expected fragment count is hardcoded.

This parser targets the English Comprehensive Rules format: numbered rules and title-case glossary headings. It excludes the introduction, contents, credits, page numbers, and repeated rulebook/section/glossary headings. It joins lines only when extracting references; document bodies retain their line breaks. Other layouts or glossary capitalization conventions need parser changes and fixtures. It preserves extraction defects such as glued words instead of attempting to rewrite the source.
