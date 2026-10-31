"""Ingest structured rulebook JSONL into a persistent cosine Chroma index."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from openai import OpenAIError

from backend.ingestion.documents import Document, Reference
from backend.ingestion.embeddings import EmbeddingConfig, OpenAIEmbeddings, validate_vectors

DEFAULT_INPUT = Path("data/processed/MagicCompRules_20260417.jsonl")
INDEX_CONFIGURATION = {"hnsw": {"space": "cosine", "ef_construction": 100,
                                     "ef_search": 100, "max_neighbors": 16}}
TEXT_FORMAT = "title-body-v1"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_text(document: Document) -> str:
    """Only title and body influence vectors; provenance remains metadata."""
    return f"{document.title.strip()}\n\n{document.text.strip()}"


def read_documents(path: Path) -> list[Document]:
    documents = []
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            record["references"] = [Reference(**ref) for ref in record.get("references", [])]
            document = Document(**record)
            if document.kind not in ("rule", "section", "glossary"):
                raise ValueError("Unknown document kind")
            if not all(isinstance(value, str) and value.strip() for value in
                       (document.id, document.version, document.text, document.title)):
                raise ValueError("ID, edition, title and body must be nonempty strings")
            if (type(document.page_start) is not int or type(document.page_end) is not int
                    or not 1 <= document.page_start <= document.page_end):
                raise ValueError("Invalid page range")
            if document.id in seen:
                raise ValueError("Duplicate document ID")
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError(f"Invalid JSONL document at line {number}: {exc}") from exc
        documents.append(document)
        seen.add(document.id)
    if not documents:
        raise ValueError("Input has no documents")
    return documents


def metadata(document: Document, config: EmbeddingConfig) -> dict:
    values = document.to_dict()
    values.pop("text")
    values.pop("id")
    # Chroma metadata cannot hold nested reference objects or null values.
    values["references"] = canonical(values["references"])
    values.update(content_hash=digest(semantic_text(document)),
                  source_hash=digest(canonical(document.to_dict())),
                  embedding_model=config.model, embedding_dimensions=config.dimensions)
    return {key: value for key, value in values.items() if value is not None}


def ingest(input_path: Path, db_path: Path, collection_name: str,
           config: EmbeddingConfig, *, embedder=None, batch_size: int = 64,
           limit: int | None = None, rebuild: bool = False) -> dict:
    if not 1 <= batch_size <= 256 or (limit is not None and limit <= 0):
        raise ValueError("Batch size must be 1..256 and limit must be positive")
    documents = read_documents(input_path)
    if limit is not None:
        documents = documents[:limit]
    embedder = embedder if embedder is not None else OpenAIEmbeddings(config)
    if embedder.config != config:
        raise ValueError("Embedder configuration does not match index configuration")
    contract = {"schema": 1, "embedding": asdict(config), "text_format": TEXT_FORMAT,
                "index": INDEX_CONFIGURATION, "chroma_version": chromadb.__version__}
    contract_json = canonical(contract)
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    try:
        collection = client.get_collection(collection_name, embedding_function=None)
    except NotFoundError:
        collection = None
    if collection is not None and rebuild:
        client.delete_collection(collection_name)
        collection = None
    if collection is None:
        collection = client.create_collection(
            collection_name, embedding_function=None, configuration=INDEX_CONFIGURATION,
            metadata={"ingestion_contract": contract_json},
        )
    actual = collection.configuration.get("hnsw") or {}
    if ((collection.metadata or {}).get("ingestion_contract") != contract_json
            or any(actual.get(key) != value for key, value in INDEX_CONFIGURATION["hnsw"].items())):
        raise ValueError("Incompatible index configuration; use a new collection or explicit --rebuild")

    stats = {"documents": len(documents), "embedded": 0, "reused": 0,
             "collection": collection_name, "versions": sorted({d.version for d in documents}),
             "configuration": contract, "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest()}
    # Cache is independent of document IDs and committed before Chroma upserts, so
    # retries after a failed write do not request the same successful batch again.
    with sqlite3.connect(db_path / "embedding-cache.sqlite3") as cache:
        cache.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")
        for offset in range(0, len(documents), batch_size):
            batch = documents[offset:offset + batch_size]
            texts = {digest(semantic_text(doc)): semantic_text(doc) for doc in batch}
            vectors, missing = {}, []
            for content_hash, text in texts.items():
                key = digest(canonical({"embedding": asdict(config), "text_format": TEXT_FORMAT,
                                        "content_hash": content_hash}))
                row = cache.execute("SELECT vector FROM embeddings WHERE key = ?", (key,)).fetchone()
                if row:
                    vector = json.loads(row[0])
                    validate_vectors([vector], 1, config.dimensions)
                    vectors[content_hash] = vector
                else:
                    missing.append((content_hash, text, key))
            if missing:
                generated = embedder.embed([item[1] for item in missing])
                validate_vectors(generated, len(missing), config.dimensions)
                for (content_hash, _, key), vector in zip(missing, generated):
                    cache.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?)", (key, canonical(vector)))
                    vectors[content_hash] = vector
                cache.commit()
            stats["embedded"] += len(missing)
            stats["reused"] += len(batch) - len(missing)
            collection.upsert(ids=[doc.id for doc in batch],
                              documents=[doc.text for doc in batch],
                              metadatas=[metadata(doc, config) for doc in batch],
                              embeddings=[vectors[digest(semantic_text(doc))] for doc in batch])
    stats["collection_count"] = collection.count()
    stats["completed_at"] = datetime.now(timezone.utc).isoformat()
    report_path = db_path / f"{collection_name}.ingestion.json"
    report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--rebuild", action="store_true", help="Delete and recreate the selected collection")
    args = parser.parse_args()
    try:
        stats = ingest(args.input, args.db, args.collection, EmbeddingConfig.from_env(),
                       batch_size=args.batch_size, limit=args.limit, rebuild=args.rebuild)
    except OpenAIError as exc:
        # Provider messages can contain request details; never print credentials.
        parser.exit(1, f"Embedding request failed ({type(exc).__name__}); completed batches remain reusable\n")
    except (OSError, ValueError, sqlite3.Error, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Ingestion failed: {exc}\n")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
