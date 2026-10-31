from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.ingestion.documents import Document
from backend.ingestion.embeddings import EmbeddingConfig
from backend.ingestion.ingest import ingest
from backend.retrieval.retrieval import Retriever
from evals.retrieval import benchmark


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
            "id": "same", "kind": "rule", "title": "Same", "text": "Aligned.",
            "page_start": 1, "page_end": 2, "version": "edition", "distance": 0., "similarity": 1.})
        self.embedder.embed.assert_called_once_with(["query"])
        self.assertEqual(len(self.retriever.search("query", k=1)), 1)

    def test_kind_and_edition_filters(self):
        results = self.retriever.search("query", kind="rule", version="edition")
        self.assertEqual([result.id for result in results], ["same", "opposite"])
        results = self.retriever.search("query", kind="glossary")
        self.assertEqual([result.id for result in results], ["orthogonal"])
        self.assertEqual(self.retriever.search("query", version="absent"), [])

    def test_exact_rule_and_id_do_not_embed(self):
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

    def test_benchmark_preserves_sources_results_and_configuration(self):
        cases = {"version": "edition", "queries": [{"query": "query", "kind": "rule",
                 "relevant_sources": [{"id": "same", "reason": "Aligned source"},
                                      {"id": "opposite", "reason": "Other source"}]}]}
        report = benchmark(self.retriever, cases, k=1)
        self.assertEqual(report["configuration"]["embedding"], {"model": "fixture-model", "dimensions": 3})
        self.assertEqual(report["queries"][0]["reviewed_source_recall_at_k"], 0.5)
        self.assertEqual(report["queries"][0]["matched_source_ids"], ["same"])
        self.assertEqual(report["queries"][0]["results"][0]["text"], "Aligned.")
        self.embedder.embed.reset_mock()
        cases["queries"][0]["relevant_sources"].append({"id": "missing"})
        with self.assertRaisesRegex(ValueError, "missing"):
            benchmark(self.retriever, cases)
        self.embedder.embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
