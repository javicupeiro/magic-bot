import json
from pathlib import Path
import tempfile
import unittest

from backend.ingestion.documents import document_id
from backend.ingestion.parse_rules import DEFAULT_INPUT, extract_references, parse_rules, parse_text, summarize

FIXTURES = Path(__file__).parent / "fixtures"
VERSION = "April 17, 2026"


def fixture(name):
    return parse_text((FIXTURES / name).read_text(encoding="utf-8"), VERSION)


class ParseRulesTests(unittest.TestCase):
    def test_subrule_example_and_page_continuation(self):
        documents = fixture("subrule_pages.txt")
        subrule = next(d for d in documents if d.rule_number == "100.1a")
        self.assertEqual((subrule.page_start, subrule.page_end), (1, 2))
        self.assertIn("Example: A player chooses\ntwo cards", subrule.text)
        self.assertIn("100.1. This is a continuation", subrule.text)
        self.assertEqual(subrule.parent_id, document_id(VERSION, "rule", "100.1"))
        self.assertEqual(documents[-1].rule_number, "100.1b")
        self.assertNotIn("Comprehensive Rules", subrule.text)

    def test_glossary_references_and_provenance(self):
        documents = fixture("glossary.txt")
        report = summarize(documents)
        entry = next(d for d in documents if d.term == "Ability")
        self.assertIsNone(entry.rule_number)
        self.assertEqual((entry.page_start, entry.page_end), (1, 2))
        self.assertIn("on the next page.", entry.text)
        self.assertEqual([(r.kind, r.identifier, r.resolved) for r in entry.references],
                         [("rule", "100.1", True), ("section", "1", True)])
        self.assertEqual([r["identifier"] for r in report["unresolved_references"]], ["999.1"])
        self.assertEqual(report["counts_by_kind"]["glossary"], 2)

    def test_contents_headers_and_numbered_examples_are_excluded(self):
        documents = fixture("contents_headers.txt")
        report = summarize(documents)
        self.assertEqual(report["counts_by_kind"], {"section": 1, "rule": 4})
        self.assertFalse(report["duplicate_ids"])
        rule = next(d for d in documents if d.rule_number == "100.1")
        self.assertEqual((rule.page_start, rule.page_end), (3, 4))
        self.assertIn("1. This ability triggers", rule.text)
        self.assertIn("100.1.", rule.text)
        self.assertNotIn("Game Concepts", rule.text)

    def test_duplicate_ids_and_empty_fragments_are_reported(self):
        documents = fixture("subrule_pages.txt")
        documents.append(documents[-1])
        documents[0].text = " "
        report = summarize(documents)
        self.assertEqual(report["duplicate_ids"], {documents[-1].id: 2})
        self.assertEqual(report["empty_fragments"], [documents[0].id])

    def test_invalid_parse_writes_report_without_replacing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, report = [Path(directory) / name for name in ("source.txt", "out.jsonl", "report.json")]
            source.write_text("1. Game Concepts\n100. General\n100.1. First.\n100.1. Duplicate.")
            output.write_text("previous valid output")
            with self.assertRaisesRegex(ValueError, "Duplicate IDs"):
                parse_rules(source, output, report, VERSION)
            self.assertEqual(output.read_text(), "previous valid output")
            self.assertTrue(json.loads(report.read_text())["duplicate_ids"])

    def test_lists_ranges_and_sections(self):
        references = extract_references(
            "See rules 100.1–100.3, 100.4a–c and 100.5. See sections 1–3. "
            "See rules 608.2k–n and 111.10k–p. See rules 119.7–8.", VERSION)
        self.assertEqual([r.identifier for r in references],
                         ["100.1", "100.2", "100.3", "100.4a", "100.4b", "100.4c", "100.5",
                          "1", "2", "3", "608.2k", "608.2m", "608.2n", "111.10k", "111.10m", "111.10n", "111.10p", "119.7", "119.8"])

    def test_lettered_rule_with_trailing_period(self):
        documents = parse_text("1. Game Concepts\n100. General\n100.1. Parent.\n100.1a. Lettered rule.", VERSION)
        self.assertEqual(documents[-1].rule_number, "100.1a")
        self.assertEqual(documents[-1].text, "Lettered rule.")

    def test_identity_is_lossless_and_independent_of_position(self):
        self.assertNotEqual(document_id(VERSION, "glossary", "A B"), document_id(VERSION, "glossary", "A-B"))
        self.assertNotEqual(document_id("other", "rule", "100.1"), document_id(VERSION, "rule", "100.1"))
        text = (FIXTURES / "subrule_pages.txt").read_text()
        self.assertEqual([d.id for d in parse_text(text, VERSION)],
                         [d.id for d in parse_text("\n\f\n" + text, VERSION)])

    def test_missing_edition_and_overlapping_paths_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            output, report = [Path(directory) / name for name in ("out.jsonl", "report.json")]
            with self.assertRaisesRegex(ValueError, "edition"):
                parse_rules(FIXTURES / "glossary.txt", output, report)
            with self.assertRaisesRegex(ValueError, "different paths"):
                parse_rules(FIXTURES / "glossary.txt", output, output, VERSION)

    def test_jsonl_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            output, report_path = [Path(directory) / name for name in ("out.jsonl", "report.json")]
            report = parse_rules(FIXTURES / "glossary.txt", output, report_path, VERSION)
            documents = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(documents), report["total"])
            entry = next(d for d in documents if d["term"] == "Ability")
            self.assertTrue(entry["references"][0]["resolved"])
            self.assertEqual(entry["version"], VERSION)
            self.assertIsNone(entry["rule_number"])
            self.assertEqual(json.loads(report_path.read_text()), report)

    @unittest.skipUnless(DEFAULT_INPUT.exists(), "Rulebook text is unavailable")
    def test_current_edition_has_valid_document_boundaries(self):
        documents = parse_text(DEFAULT_INPUT.read_text(encoding="utf-8"), VERSION)
        report = summarize(documents)
        self.assertFalse(report["duplicate_ids"])
        self.assertFalse(report["empty_fragments"])
        self.assertFalse(report["unresolved_parents"])
        self.assertEqual(set(report["counts_by_kind"]), {"section", "rule", "glossary"})
        brawl = next(d for d in documents if d.rule_number == "119.1d")
        self.assertTrue(brawl.text.startswith("In a two-player Brawl game"))


if __name__ == "__main__":
    unittest.main()
