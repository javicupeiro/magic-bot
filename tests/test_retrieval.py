from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.ingestion.documents import Document, Reference
from backend.ingestion.embeddings import EmbeddingConfig
from backend.ingestion.ingest import ingest
from backend.retrieval.hybrid import BM25Index, reciprocal_rank_fusion, tokenize
from backend.retrieval.retrieval import Retriever
from backend.retrieval.benchmark import benchmark, sweep


class KnownEmbeddings:
    def __init__(self):
        self.config = EmbeddingConfig(model="fixture-model", dimensions=3)
        self.embed = Mock(side_effect=lambda texts: [
            {"Same\n\nAligned.": [1., 0., 0.], "Orthogonal\n\nPerpendicular.": [0., 1., 0.],
             "Opposite\n\nOpposed.": [-1., 0., 0.], "query": [1., 0., 0.]}[text]
            for text in texts])


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "chroma"
        self.source = self.root / "rules.jsonl"
        self.embedder = KnownEmbeddings()
        self.documents = [
            Document("same", "rule", "Aligned.", "edition", 1, 2, "Same", rule_number="702.49a"),
            Document("orthogonal", "glossary", "Perpendicular.", "edition", 3, 3, "Orthogonal", term="Term"),
            Document("opposite", "rule", "Opposed.", "edition", 4, 4, "Opposite", rule_number="100.1"),
        ]
        self.write_and_ingest()
        self.embedder.embed.reset_mock()
        self.retriever = Retriever(self.db, "rules", embedder=self.embedder)

    def write_and_ingest(self):
        self.source.write_text("".join(json.dumps(doc.to_dict()) + "\n" for doc in self.documents))
        ingest(self.source, self.db, "rules", self.embedder.config, embedder=self.embedder)

    def test_known_cosine_distances_and_order(self):
        results = self.retriever.search(" query ", k=100)
        self.assertEqual([result.id for result in results], ["same", "orthogonal", "opposite"])
        for result, distance, similarity in zip(results, [0., 1., 2.], [1., 0., -1.]):
            self.assertAlmostEqual(result.distance, distance, places=6)
            self.assertAlmostEqual(result.similarity, similarity, places=6)
        self.assertEqual(results[0].to_dict(), {
            "id": "same", "kind": "rule", "title": "Same", "text": "Aligned.", "references": [],
            "page_start": 1, "page_end": 2, "version": "edition", "distance": 0., "similarity": 1.,
            "retrieval_method": "vector", "ranks": {"vector": 1}, "fusion_score": None})
        self.embedder.embed.assert_called_once_with(["query"])
        self.assertEqual(len(self.retriever.search("query", k=1)), 1)

    def test_kind_and_edition_filters(self):
        results = self.retriever.search("query", kind="rule", version="edition")
        self.assertEqual([result.id for result in results], ["same", "opposite"])
        results = self.retriever.search("query", kind="glossary")
        self.assertEqual([result.id for result in results], ["orthogonal"])
        self.assertEqual(self.retriever.search("query", version="absent"), [])

    def test_exact_rule_and_id_do_not_embed(self):
        self.documents[1].references = [Reference("rule", "702.49a", "same", True)]
        self.write_and_ingest()
        self.embedder.embed.reset_mock()
        self.assertEqual(self.retriever.get("orthogonal")[0].references, self.documents[1].references)
        self.documents.append(replace(self.documents[0], id="same-new", version="new edition"))
        self.write_and_ingest()
        self.embedder.embed.reset_mock()
        self.assertEqual([result.id for result in self.retriever.rule("702.49a")], ["same", "same-new"])
        result, = self.retriever.rule("702.49a", version="edition")
        self.assertIsNone(result.distance)
        self.assertIsNone(result.similarity)
        self.assertEqual(self.retriever.get("same"), [result])
        self.assertEqual(self.retriever.get("same", kind="glossary"), [])
        self.assertEqual(self.retriever.get("same", version="absent"), [])
        self.assertEqual(self.retriever.rule("702.49"), [])
        self.assertEqual(self.retriever.get("missing"), [])
        self.embedder.embed.assert_not_called()

    def test_rank_fusion_deduplicates_and_breaks_ties_by_id(self):
        """One-based positions contribute once per branch; raw scores are absent."""
        fused = reciprocal_rank_fusion({"vector": ["b", "a", "b"], "bm25": ["a", "b", "c"]})
        self.assertEqual([identifier for identifier, _, _ in fused], ["a", "b", "c"])
        self.assertAlmostEqual(fused[0][1], 1 / 61 + 1 / 62)
        self.assertEqual(fused[1][2], {"vector": 1, "bm25": 2})
        self.assertAlmostEqual(fused[2][1], 1 / 63)
        self.assertEqual(reciprocal_rank_fusion({"bm25": ["a", "b", "c"],
                                               "vector": ["b", "a", "b"]}), fused)

    def test_hybrid_keeps_lexical_only_sources_filters_and_exact_path(self):
        """A lexical winner outside the vector pool still carries original evidence."""
        self.documents[1].references = [Reference("rule", "702.49a", "same", True)]
        self.write_and_ingest()
        self.retriever.source_path = self.source
        self.retriever.candidate_limit = 1
        results = self.retriever.search("query", k=1, method="hybrid")
        self.assertEqual(results[0].ranks, {"vector": 1})
        # The fixture query vector remains aligned; lexical evidence instead
        # chooses the orthogonal glossary record. ID breaks their RRF tie.
        self.embedder.embed.side_effect = lambda texts: [[1., 0., 0.] for _ in texts]
        result, = self.retriever.search("Perpendicular", k=1, method="hybrid")
        self.assertEqual(result.id, "orthogonal")
        self.assertEqual(result.ranks, {"bm25": 1})
        self.assertIsNone(result.distance)
        self.assertEqual(result.text, "Perpendicular.")
        self.assertEqual(result.references, self.documents[1].references)
        self.assertEqual((result.version, result.page_start), ("edition", 3))
        self.assertEqual(len(self.retriever.last_search["candidates"]), 2)
        result, = self.retriever.search("Perpendicular", k=1, kind="rule", method="hybrid")
        self.assertEqual(result.id, "same")
        self.assertEqual(self.retriever.search("query", version="absent", method="hybrid"), [])
        selected = self.retriever.search("Perpendicular", k=1, version="edition", method="hybrid")
        self.assertEqual(selected[0].id, "orthogonal")
        self.embedder.embed.reset_mock()
        self.retriever.method = "hybrid"
        self.retriever.source_path = self.root / "missing.jsonl"
        exact, = self.retriever.rule("702.49a")
        self.assertIsNone(exact.retrieval_method)
        self.embedder.embed.assert_not_called()

    def test_hybrid_rejects_edition_content_and_partial_index_mismatch(self):
        """Matching IDs alone cannot establish that both indexes share a corpus."""
        self.retriever.source_path = self.source
        original = self.documents[0]
        for changed in (replace(original, version="different edition"),
                        replace(original, text="Changed source.")):
            self.documents[0] = changed
            self.source.write_text("".join(json.dumps(doc.to_dict()) + "\n" for doc in self.documents))
            with self.assertRaisesRegex(ValueError, "does not match Chroma"):
                self.retriever.search("query", method="hybrid")
        self.documents[0] = original
        self.write_and_ingest()
        self.retriever.collection.delete(ids=["opposite"])
        self.embedder.embed.reset_mock()
        with self.assertRaisesRegex(ValueError, "does not match Chroma"):
            self.retriever.search("query", method="hybrid")
        self.embedder.embed.assert_not_called()

    def test_lexical_tokenization_and_kind_filter_before_limit(self):
        """Preserve rule IDs and count term frequency without returning nonmatches."""
        self.assertEqual(tokenize("Rule 702.49a: NINJUTSU, ¿maná?"),
                         ["rule", "702.49a", "ninjutsu", "maná"])
        index = BM25Index(self.documents)
        self.assertEqual(index.search("702.49a", limit=1), ["same"])
        self.assertEqual(index.search("Perpendicular", limit=1, kind="rule"), [])
        self.assertEqual(index.search("unmatched", limit=3), [])

    def test_configuration_is_read_from_index(self):
        with patch("backend.retrieval.retrieval.OpenAIEmbeddings") as provider:
            retriever = Retriever(self.db, "rules")
        provider.assert_called_once_with(self.embedder.config)
        self.assertEqual(retriever.config, self.embedder.config)
        with self.assertRaisesRegex(ValueError, "does not match"):
            Retriever(self.db, "rules", embedder=Mock(config=EmbeddingConfig(dimensions=4)))
        self.retriever.collection.modify(metadata={"ingestion_contract": "{}"})
        with self.assertRaisesRegex(ValueError, "configuration"):
            Retriever(self.db, "rules")
        self.embedder.embed.assert_not_called()

    def test_validation_and_empty_index(self):
        for query, options in [(" ", {}), ("query", {"k": 0}), ("query", {"k": -1}),
                               ("query", {"kind": "unknown"})]:
            with self.assertRaises(ValueError):
                self.retriever.search(query, **options)
        with self.assertRaisesRegex(ValueError, "empty"):
            self.retriever.rule(" ")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.retriever.get(" ")
        self.retriever.collection.delete(ids=[doc.id for doc in self.documents])
        self.assertEqual(self.retriever.search("query"), [])
        self.embedder.embed.assert_not_called()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            Retriever(self.root / "missing", "rules")
        self.assertFalse((self.root / "missing").exists())

    def test_cli_exact_lookup_in_fresh_process(self):
        process = subprocess.run([sys.executable, "-m", "backend.retrieval.retrieval", "--db", str(self.db),
                                  "--collection", "rules", "--rule", "702.49a"],
                                 capture_output=True, text=True, check=True)
        result, = json.loads(process.stdout)["results"]
        self.assertEqual(result["id"], "same")
        self.assertIsNone(result["distance"])

    def test_sweep_varies_limits_and_retains_repeated_measurements(self):
        """The matrix uses real retrieval with fake embeddings and unchanged judgments."""
        self.retriever.source_path = self.source
        cases = {"version": "edition", "queries": [{"query": "query",
                 "relevant_sources": [{"id": "same"}, {"id": "opposite"}]}]}
        report = sweep(self.retriever, cases, repeats=2)
        self.assertEqual(report["configuration_count"], 16)
        self.assertEqual(len(report["schedule"]), 32)
        self.assertEqual(self.embedder.embed.call_count, 32)
        self.assertEqual(report["configurations"]["vector-k5"]["summary"]["mean_recall_at_k"], 1)
        configuration = report["configurations"]["hybrid-k8-c80"]
        self.assertEqual(configuration["first_run"]["queries"][0]["search"]["candidate_limit"], 80)
        self.assertEqual(configuration["summary"]["query_measurements"], 2)
        self.assertEqual(configuration["summary"]["rankings_changed_from_first"], 0)
        self.assertEqual(configuration["samples"][0]["result_ids"], ["same", "orthogonal", "opposite"])
        self.assertEqual(configuration["first_run"]["queries"][0]["results"][0]["text"], "Aligned.")
        with self.assertRaisesRegex(ValueError, "repeats"):
            sweep(self.retriever, cases, repeats=0)

    def test_benchmark_preserves_sources_results_and_configuration(self):
        cases = {"version": "edition", "queries": [{"query": "query", "kind": "rule",
                 "relevant_sources": [{"id": "same", "reason": "Aligned source"},
                                      {"id": "opposite", "reason": "Other source"}]}]}
        report = benchmark(self.retriever, cases, k=1)
        self.assertEqual(report["configuration"]["embedding"], {"model": "fixture-model", "dimensions": 3})
        self.assertEqual(report["queries"][0]["reviewed_source_recall_at_k"], 0.5)
        self.assertEqual(report["queries"][0]["matched_source_ids"], ["same"])
        self.assertEqual(report["queries"][0]["results"][0]["text"], "Aligned.")
        self.assertGreater(report["queries"][0]["latency_ms"], 0)
        self.assertGreater(report["summary"]["mean_context_characters"], 0)
        self.assertEqual(report["summary"]["hit_rate"], 1)
        self.assertEqual(report["summary"]["full_recall_rate"], 0)
        self.assertEqual(report["summary"]["mean_reciprocal_rank"], 1)
        self.retriever.source_path = self.source
        hybrid = benchmark(self.retriever, cases, k=1, method="hybrid")
        self.assertEqual(hybrid["method"], "hybrid")
        self.assertIn("fusion_score", hybrid["queries"][0]["search"]["candidates"][0])
        self.embedder.embed.reset_mock()
        cases["queries"][0]["relevant_sources"].append({"id": "missing"})
        with self.assertRaisesRegex(ValueError, "missing"):
            benchmark(self.retriever, cases)
        self.embedder.embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
