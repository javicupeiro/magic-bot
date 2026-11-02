"""Export/restore rules-only Chroma records without native HNSW files or secrets.

The portable gzip JSONL bundle is rebuilt into a native index at image build time.
Neither operation calls an embedding provider. Do not run against a live writer.
"""

import argparse
import gzip
import json
from pathlib import Path

import chromadb
from chromadb.config import Settings


def client(path):
    return chromadb.PersistentClient(str(path), settings=Settings(anonymized_telemetry=False))


def export_index(source, bundle):
    collection = client(source).get_collection("magic-rules", embedding_function=None)
    count = collection.count()
    if not count:
        raise ValueError("Cannot export an empty rules index")
    header = {"schema": 1, "collection": "magic-rules", "count": count,
              "metadata": collection.metadata, "configuration": collection.configuration}
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(bundle, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")
        for offset in range(0, count, 64):
            page = collection.get(limit=64, offset=offset,
                                  include=["documents", "metadatas", "embeddings"])
            for identifier, text, metadata, embedding in zip(
                    page["ids"], page["documents"], page["metadatas"], page["embeddings"]):
                stream.write(json.dumps({"id": identifier, "document": text,
                                         "metadata": metadata, "embedding": embedding.tolist()},
                                        ensure_ascii=False, separators=(",", ":")) + "\n")
    return count


def restore_index(bundle, destination):
    if destination.exists():
        raise ValueError("Restore requires a new index directory")
    with gzip.open(bundle, "rt", encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if header["schema"] != 1 or header["collection"] != "magic-rules" or header["count"] <= 0:
            raise ValueError("Invalid rules bundle")
        contract = json.loads(header["metadata"]["ingestion_contract"])
        if contract["chroma_version"] != chromadb.__version__:
            raise ValueError("Bundle requires the same locked Chroma version")
        collection = client(destination).create_collection(
            "magic-rules", embedding_function=None, metadata=header["metadata"],
            configuration=header["configuration"])
        batch = []
        count = 0

        def insert(records):
            collection.add(ids=[r["id"] for r in records],
                           documents=[r["document"] for r in records],
                           metadatas=[r["metadata"] for r in records],
                           embeddings=[r["embedding"] for r in records])

        for line in stream:
            batch.append(json.loads(line))
            count += 1
            if len(batch) == 64:
                insert(batch)
                batch = []
        if batch:
            insert(batch)
        if count != header["count"] or collection.count() != count:
            raise ValueError("Incomplete rules bundle")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "restore"])
    parser.add_argument("--bundle", type=Path, default=Path("deploy/rules.jsonl.gz"))
    parser.add_argument("--index", type=Path, default=Path("data/chroma"))
    args = parser.parse_args()
    count = (export_index(args.index, args.bundle) if args.operation == "export"
             else restore_index(args.bundle, args.index))
    print(f"{args.operation}: {count} rules records")
