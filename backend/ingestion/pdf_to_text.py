"""Extract the existing PDF text layer without OCR or content corrections."""

import argparse
import hashlib
import json
import re
import sys
from io import BytesIO
from pathlib import Path

import pypdf

EXTRACTOR_VERSION = "1"
PAGE_SEPARATOR = "\n\f\n"
# Keep empty pages and physical boundaries recoverable by the rule parser.
DEFAULT_INPUT = Path("data/game_rules/MagicCompRules_20260417.pdf")


def extract_pdf(input_path: Path, output_path: Path, manifest_path: Path) -> dict:
    """Extract the PDF's existing text into UTF-8 and write a provenance manifest.

    Preserve physical page order, including empty pages. Warnings flag suspect
    extraction without correcting text or attempting OCR; the PDF is untouched.
    """
    paths = [path.resolve() for path in (input_path, output_path, manifest_path)]
    if len(set(paths)) != 3:
        raise ValueError("Input, text output, and manifest must be different paths")

    pdf_bytes = input_path.read_bytes()
    # Use the same bytes for hashing and extraction, even if the file changes on disk.
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    pages = []
    warnings = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text(extraction_mode="plain") or ""
        pages.append(text)
        # These heuristics flag review candidates; they do not assess text accuracy.
        reasons = []
        if not text.strip():
            reasons.append("empty text layer; OCR was not attempted")
        elif len(text.strip()) < 40:
            reasons.append("unusually short text layer (fewer than 40 characters)")
        if "\ufffd" in text or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", text):
            reasons.append("replacement or unexpected control characters")
        if reasons:
            warnings.append({"page": page_number, "reasons": reasons})

    # Read the source's declared edition, rather than guessing from its filename.
    edition_match = re.search(r"These rules are effective as of\s+([^\n]+)", "\n".join(pages[:2]))
    declared_edition = edition_match.group(1).strip().rstrip(".") if edition_match else None
    if declared_edition is None:
        warnings.append({"page": None, "reasons": ["declared edition not found"]})

    # Record enough extraction settings and source identity to reproduce the TXT.
    manifest = {
        "source_pdf": str(input_path),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "declared_edition": declared_edition,
        "page_count": len(pages),
        "extractor": {"name": "backend.ingestion.pdf_to_text", "version": EXTRACTOR_VERSION,
                      "pypdf_version": pypdf.__version__, "mode": "plain"},
        "text_output": str(output_path),
        "encoding": "utf-8",
        "page_separator": PAGE_SEPARATOR,
        "page_character_counts": [len(page) for page in pages],
        "warnings": warnings,
    }
    for path in (output_path, manifest_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(PAGE_SEPARATOR.join(pages), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    """Run extraction, e.g. uv run --locked pdf-to-text --input path/rules.pdf."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    output = args.output or Path("data/processed") / args.input.with_suffix(".txt").name
    manifest_path = args.manifest or output.with_suffix(".manifest.json")
    try:
        manifest = extract_pdf(args.input, output, manifest_path)
    except (OSError, ValueError, pypdf.errors.PyPdfError) as exc:
        parser.exit(1, f"Extraction failed: {exc}\n")
    for warning in manifest["warnings"]:
        print(f"Warning: page {warning['page']}: {'; '.join(warning['reasons'])}", file=sys.stderr)
    print(f"Extracted {manifest['page_count']} pages to {output}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
