# Magic bot

Incremental implementation. This first step extracts the rulebook's existing text layer without OCR, language models, or rule parsing.

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
