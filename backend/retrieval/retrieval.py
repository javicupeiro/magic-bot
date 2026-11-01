"""Retrieve source records from a persisted rulebook index."""

from dataclasses import asdict, dataclass, field
import json

from backend.ingestion.documents import Reference
import argparse
import json
from pathlib import Path

import chromadb
from chromadb.config import Settings
from openai import OpenAIError

from backend.ingestion.embeddings import EmbeddingConfig, OpenAIEmbeddings, validate_vectors
from backend.ingestion.ingest import INDEX_CONFIGURATION, TEXT_FORMAT

KINDS = ("rule", "section", "glossary")
DISTANCE_EXPLANATION = (
    "Chroma 1.5.5 cosine distance: d = 1 - cosine similarity; lower is closer. "
    "Similarity = 1 - d. These values are not probabilities or confidence scores. "
    "Exact lookups have no distance or similarity."
)


@dataclass(frozen=True)
class SearchResult:
    """Original source content and provenance, with a distance for vector queries."""

    id: str
    kind: str
    title: str
    text: str
    page_start: int
    page_end: int
    version: str
    distance: float | None = None
    references: list[Reference] = field(default_factory=list)

    @property
    def similarity(self) -> float | None:
        """Convert cosine distance to similarity; exact lookups have neither score."""
        return None if self.distance is None else 1.0 - self.distance

    def to_dict(self) -> dict:
        """Include the derived similarity when serializing a result for callers."""
        return {**asdict(self), "similarity": self.similarity}


def _results(records, *, vector: bool = False) -> list[SearchResult]:
    """Normalize Chroma's query and get response shapes into the same record type."""
    # query nests results per input vector; our calls use exactly one query vector.
    # get returns flat arrays and performs no distance calculation.
    ids = records["ids"][0] if vector else records["ids"]
    documents = records["documents"][0] if vector else records["documents"]
    metadatas = records["metadatas"][0] if vector else records["metadatas"]
    distances = records["distances"][0] if vector else [None] * len(ids)
    return [SearchResult(identifier, meta["kind"], meta["title"], text,
                         meta["page_start"], meta["page_end"], meta["version"], distance,
                         [Reference(**ref) for ref in json.loads(meta.get("references", "[]"))])
            for identifier, text, meta, distance in zip(ids, documents, metadatas, distances)]


def _where(kind=None, version=None):
    """Build optional metadata restrictions using Chroma's explicit AND syntax."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"Unknown kind: {kind}")
    filters = [{key: value} for key, value in (("kind", kind), ("version", version))
               if value is not None]
    return {"$and": filters} if len(filters) > 1 else (filters[0] if filters else None)


class Retriever:
    """Read a persisted index through semantic search or exact source lookups.

    Reuse the index's saved embedding settings instead of current environment
    defaults: query vectors must occupy the same space as the stored vectors.
    """

    def __init__(self, db_path: Path = Path("data/chroma"), collection_name: str = "magic-rules",
                 *, embedder=None):
        """Open an existing collection and verify its ingestion contract first."""
        if not db_path.is_dir():
            raise ValueError(f"Index directory does not exist: {db_path}; ingest documents first")
        self.client = chromadb.PersistentClient(str(db_path), settings=Settings(anonymized_telemetry=False))
        self.collection = self.client.get_collection(collection_name, embedding_function=None)
        try:
            # Verify both saved settings and actual HNSW configuration before use.
            self.contract = json.loads((self.collection.metadata or {})["ingestion_contract"])
            self.config = EmbeddingConfig(**self.contract["embedding"])
            compatible = (self.contract["schema"] == 1
                          and self.contract["text_format"] == TEXT_FORMAT
                          and self.contract["chroma_version"] == chromadb.__version__
                          and self.contract["index"] == INDEX_CONFIGURATION
                          and all((self.collection.configuration.get("hnsw") or {}).get(key) == value
                                  for key, value in INDEX_CONFIGURATION["hnsw"].items()))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Missing or invalid ingestion configuration") from exc
        if not compatible:
            raise ValueError("Incompatible ingestion configuration; recreate the index with the current ingestion tool")
        if embedder is not None and embedder.config != self.config:
            raise ValueError("Embedder configuration does not match index configuration")
        self.embedder = embedder if embedder is not None else OpenAIEmbeddings(self.config)

    def search(self, query: str, *, k: int = 5, kind: str | None = None,
               version: str | None = None) -> list[SearchResult]:
        """Embed an English query and return up to k nearest filtered sources.

        Chroma orders by cosine distance, lower first. No score threshold or
        reranker is applied, so returned neighbors are not guaranteed evidence.
        """
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        if not query.strip():
            raise ValueError("Query must not be empty")
        where = _where(kind, version)
        count = self.collection.count()
        if count == 0:
            # Avoid a paid embedding request when the collection has no documents.
            return []
        vectors = self.embedder.embed([query.strip()])
        validate_vectors(vectors, 1, self.config.dimensions)
        # Restrict metadata inside the database query, before selecting neighbors.
        return _results(self.collection.query(query_embeddings=vectors, n_results=min(k, count),
                                             where=where, include=["documents", "metadatas", "distances"]),
                        vector=True)

    def get(self, identifier: str, *, kind: str | None = None,
            version: str | None = None) -> list[SearchResult]:
        """Fetch an exact document ID, optionally constrained by kind and edition.

        Uses stored records only: no embeddings, network request or similarity score.
        """
        if not identifier.strip():
            raise ValueError("Document ID must not be empty")
        return _results(self.collection.get(ids=[identifier], where=_where(kind, version),
                                            include=["documents", "metadatas"]))

    def rule(self, number: str, *, version: str | None = None) -> list[SearchResult]:
        """Fetch a rule number in all indexed editions, or one explicit edition.

        Example: rule('702.49a'). This is an exact metadata lookup, so glossary
        terms and nearby rule numbers cannot match. No embedding is generated.
        """
        if not number.strip():
            raise ValueError("Rule number must not be empty")
        filters = [{"kind": "rule"}, {"rule_number": number.strip()}]
        if version is not None:
            filters.append({"version": version})
        records = self.collection.get(where={"$and": filters}, include=["documents", "metadatas"])
        return sorted(_results(records), key=lambda result: result.id)


def main() -> None:
    """Print source JSON using search-rules 'query', --rule 702.49a or --id ID."""
    parser = argparse.ArgumentParser(description=__doc__, epilog=DISTANCE_EXPLANATION)
    mode = parser.add_mutually_exclusive_group(required=True)
    # Exactly one lookup mode is required; only the free-text query needs embeddings.
    mode.add_argument("query", nargs="?", help="English semantic search query")
    mode.add_argument("--rule", help="Exact rule number, e.g. 702.49a; no embedding request")
    mode.add_argument("--id", help="Exact document ID; no embedding request")
    parser.add_argument("-k", type=int, default=5, help="Positive number of semantic search results")
    parser.add_argument("--kind", choices=KINDS)
    parser.add_argument("--version", help="Filter by exact rulebook edition")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    args = parser.parse_args()
    if args.rule is not None and args.kind not in (None, "rule"):
        parser.error("--rule requires --kind rule or no kind filter")
    try:
        retriever = Retriever(args.db, args.collection)
        if args.rule is not None:
            results = retriever.rule(args.rule, version=args.version)
        elif args.id is not None:
            results = retriever.get(args.id, kind=args.kind, version=args.version)
        else:
            results = retriever.search(args.query, k=args.k, kind=args.kind, version=args.version)
    except OpenAIError as exc:
        parser.exit(1, f"Embedding request failed ({type(exc).__name__})\n")
    except (OSError, ValueError, chromadb.errors.ChromaError) as exc:
        parser.exit(1, f"Retrieval failed: {exc}\n")
    print(json.dumps({"distance_explanation": DISTANCE_EXPLANATION,
                      "results": [result.to_dict() for result in results]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
