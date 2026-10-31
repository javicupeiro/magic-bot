from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import chromadb
from chromadb.config import Settings

from backend.ingestion.documents import Document, Reference
from backend.ingestion.embeddings import EmbeddingConfig, OpenAIEmbeddings
from backend.ingestion.ingest import ingest, semantic_text


class FakeEmbeddings:
    def __init__(self, config):
        self.config = config
        self.texts = []

    def embed(self, texts):
        self.texts.extend(texts)
        return [[(byte + 1) / 256 for byte in hashlib.sha256(text.encode()).digest()]
                [:self.config.dimensions] for text in texts]


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / 'rules.jsonl'
        self.db = self.root / 'chroma'
        self.config = EmbeddingConfig(dimensions=3)
        self.embedder = FakeEmbeddings(self.config)
        self.documents = [
            Document('rule-1', 'rule', 'A player may act.', 'edition', 1, 2, 'Actions',
                     [Reference('rule', '100.2', 'rule-2', True)], rule_number='100.1'),
            Document('term-1', 'glossary', 'An object in the game.', 'edition', 3, 3, 'Object', term='Object'),
        ]
        self.write()

    def write(self):
        self.source.write_text(''.join(json.dumps(d.to_dict()) + '\n' for d in self.documents))

    def run_ingest(self, **options):
        return ingest(self.source, self.db, 'rules', self.config, embedder=self.embedder, **options)

    def collection(self):
        return chromadb.PersistentClient(str(self.db), settings=Settings(anonymized_telemetry=False)).get_collection(
            'rules', embedding_function=None)

    def test_persistence_reingestion_and_metadata(self):
        first = self.run_ingest(limit=1, batch_size=1)
        full = self.run_ingest(batch_size=1)
        repeated = self.run_ingest(limit=1)
        self.assertEqual((first['embedded'], full['embedded'], repeated['embedded']), (1, 1, 0))
        self.assertEqual(repeated['collection_count'], 2)
        self.assertEqual(len(self.embedder.texts), 2)
        record = self.collection().get(ids=['rule-1'], include=['documents', 'metadatas'])
        self.assertEqual(record['documents'], [self.documents[0].text])
        self.assertEqual(json.loads(record['metadatas'][0]['references']), [self.documents[0].references[0].__dict__])
        self.assertNotIn('term', record['metadatas'][0])
        self.assertEqual(record['metadatas'][0]['version'], 'edition')
        code = '''import chromadb, sys
from chromadb.config import Settings
c=chromadb.PersistentClient(sys.argv[1], settings=Settings(anonymized_telemetry=False)).get_collection('rules', embedding_function=None)
assert c.count() == 2
assert c.get(ids=['rule-1'])['documents'] == ['A player may act.']
'''
        subprocess.run([sys.executable, '-c', code, str(self.db)], check=True, capture_output=True)

    def test_metadata_changes_and_identical_content_reuse_vectors(self):
        self.run_ingest()
        self.documents[0] = replace(self.documents[0], page_start=2, page_end=4)
        self.documents.append(replace(self.documents[0], id='rule-copy', version='new edition'))
        self.write()
        self.assertEqual(self.run_ingest()['embedded'], 0)
        self.assertEqual(self.collection().get(ids=['rule-1'])['metadatas'][0]['page_end'], 4)
        self.documents[0].text = 'Changed body.'
        self.write()
        self.assertEqual(self.run_ingest()['embedded'], 1)
        self.documents[0].text = 'A player may act.'
        self.write()
        self.assertEqual(self.run_ingest()['embedded'], 0)

    def test_incompatible_model_dimensions_format_and_metric(self):
        self.run_ingest()
        for config in (EmbeddingConfig(model='different', dimensions=3), EmbeddingConfig(dimensions=4)):
            with self.assertRaisesRegex(ValueError, 'Incompatible'):
                ingest(self.source, self.db, 'rules', config, embedder=FakeEmbeddings(config))
        self.collection().modify(metadata={'ingestion_contract': 'unknown'})
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            self.run_ingest()
        self.assertEqual(self.collection().count(), 2)
        self.assertEqual(self.run_ingest(rebuild=True)['embedded'], 0)
        client = chromadb.PersistentClient(str(self.db), settings=Settings(anonymized_telemetry=False))
        contract = self.collection().metadata
        client.delete_collection('rules')
        client.create_collection('rules', metadata=contract, embedding_function=None,
                                 configuration={'hnsw': {'space': 'l2'}})
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            self.run_ingest()

    def test_cached_run_needs_no_provider_and_rebuild_uses_new_dimensions(self):
        self.run_ingest()
        with patch('backend.ingestion.embeddings.OpenAI', side_effect=AssertionError('Unexpected API call')):
            stats = ingest(self.source, self.db, 'rules', self.config)
        self.assertEqual(stats['embedded'], 0)
        config = EmbeddingConfig(dimensions=4)
        stats = ingest(self.source, self.db, 'rules', config,
                       embedder=FakeEmbeddings(config), rebuild=True)
        self.assertEqual(stats['embedded'], 2)
        record = self.collection().get(ids=['rule-1'], include=['embeddings'])
        self.assertEqual(len(record['embeddings'][0]), 4)

    def test_failed_upsert_can_retry_without_provider(self):
        with patch('chromadb.api.models.Collection.Collection.upsert', side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                self.run_ingest()
        self.assertEqual(self.run_ingest()['embedded'], 0)
        self.assertEqual(self.collection().count(), 2)

    def test_invalid_vectors_and_duplicate_input(self):
        self.embedder.embed = Mock(return_value=[[1.0]])
        with self.assertRaisesRegex(ValueError, 'invalid embedding'):
            self.run_ingest()
        self.assertEqual(self.collection().count(), 0)
        self.documents.append(self.documents[0])
        self.write()
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.run_ingest(rebuild=True)

    def test_semantic_text_excludes_provenance(self):
        document = self.documents[0]
        self.assertEqual(semantic_text(document), 'Actions\n\nA player may act.')
        self.assertEqual(semantic_text(document), semantic_text(replace(document, id='another', version='new', page_end=99)))


class ProviderTests(unittest.TestCase):
    def test_environment_and_explicit_provider_arguments(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only', 'OPENAI_EMBEDDING_MODEL': 'text-embedding-3-small',
                                     'OPENAI_EMBEDDING_DIMENSIONS': '3'}), patch('backend.ingestion.embeddings.load_dotenv'), \
                patch('backend.ingestion.embeddings.OpenAI') as client:
            config = EmbeddingConfig.from_env()
            client.return_value.embeddings.create.return_value.data = [
                Mock(index=1, embedding=[0., 1., 0.]), Mock(index=0, embedding=[1., 0., 0.])]
            provider = OpenAIEmbeddings(config)
            self.assertEqual(provider.embed(['first', 'second']), [[1., 0., 0.], [0., 1., 0.]])
            client.return_value.embeddings.create.assert_called_once_with(
                model='text-embedding-3-small', dimensions=3, input=['first', 'second'], encoding_format='float')


if __name__ == '__main__':
    unittest.main()
