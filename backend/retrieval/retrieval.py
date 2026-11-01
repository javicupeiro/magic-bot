"""Serve vector/hybrid search and exact lookups to CLIs and rules workflows.

Reads persisted Chroma sources; free-text searches call the embedding provider.
Hybrid search validates and builds an in-memory lexical index from parsed JSONL.
"""

from dataclasses import asdict, dataclass, field, replace
import argparse
import json
from pathlib import Path

import chromadb
from chromadb.config import Settings
from openai import OpenAIError

from backend.ingestion.documents import Reference
from backend.ingestion.embeddings import EmbeddingConfig, OpenAIEmbeddings, validate_vectors
from backend.ingestion.ingest import (DEFAULT_INPUT, INDEX_CONFIGURATION, TEXT_FORMAT,
                                      canonical, digest, read_documents)
from backend.retrieval.hybrid import BM25Index, DEFAULT_CANDIDATES, reciprocal_rank_fusion

METHODS = ("vector", "hybrid")
# The paired evaluation found equal mean recall and individual regressions.
# Keep vector as the default; hybrid remains an explicit, measurable option.
DEFAULT_METHOD = "vector"
# The measured sweep found the highest reviewed-source recall at twenty hits.
DEFAULT_K = 20

KINDS = ("rule", "section", "glossary")
DISTANCE_EXPLANATION = (
    "Chroma 1.5.5 cosine distance: d = 1 - cosine similarity; lower is closer. "
    "Similarity = 1 - d. These values are not probabilities or confidence scores. "
    "Exact lookups and lexical-only hybrid hits have no distance or similarity. "
    "Hybrid order uses reciprocal ranks, not cosine distance or raw BM25 scores."
)


@dataclass(frozen=True)
class SearchResult:
    """Original source evidence plus optional retrieval method and ranking positions.

    distance exists only for hits in the vector candidate list. fusion_score
    exists only for hybrid search; exact lookups have no ranking provenance.
    """

    id: str
    kind: str
    title: str
    text: str
    page_start: int
    page_end: int
    version: str
    distance: float | None = None
    references: list[Reference] = field(default_factory=list)
    retrieval_method: str | None = None
    ranks: dict[str, int] = field(default_factory=dict)
    fusion_score: float | None = None

    @property
    def similarity(self) -> float | None:
        """Convert available cosine distance; lexical-only/exact hits have no score."""
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
    """Read a persisted index through vector/hybrid search or exact source lookups.

    Reuse the index's saved embedding settings instead of current environment
    defaults: query vectors must occupy the same space as the stored vectors.
    """

    def __init__(self, db_path: Path = Path("data/chroma"), collection_name: str = "magic-rules",
                 *, embedder=None, source_path: Path = DEFAULT_INPUT,
                 method: str = DEFAULT_METHOD, candidate_limit: int = DEFAULT_CANDIDATES):
        """Open an existing collection and verify its ingestion contract first."""
        if method not in METHODS:
            raise ValueError(f"Unknown retrieval method: {method}")
        if type(candidate_limit) is not int or candidate_limit <= 0:
            raise ValueError("candidate_limit must be a positive integer")
        self.method, self.source_path = method, source_path
        self.candidate_limit = candidate_limit
        self.lexical_indexes = {}
        self.last_search = {}
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

    def _lexical_index(self, version: str | None) -> BM25Index:
        """Validate IDs, editions and complete source hashes before indexing JSONL.

        Check the selected edition, or the entire collection when unfiltered.
        Partial ingestion, stale content and metadata changes fail before paid
        query embedding. Cache only in memory; do not ingest concurrently.
        """
        if version not in self.lexical_indexes:
            documents = [document for document in read_documents(self.source_path)
                         if version is None or document.version == version]
            records = self.collection.get(where=_where(version=version), include=["metadatas"])
            expected = {document.id: (document.version, digest(canonical(document.to_dict())))
                        for document in documents}
            actual = {identifier: (meta["version"], meta.get("source_hash"))
                      for identifier, meta in zip(records["ids"], records["metadatas"])}
            if actual != expected:
                raise ValueError("BM25 corpus does not match Chroma IDs, editions or source hashes; "
                                 "select the matching JSONL/edition or reingest the complete corpus")
            self.lexical_indexes[version] = BM25Index(documents)
        return self.lexical_indexes[version]

    def search(self, query: str, *, k: int = DEFAULT_K, kind: str | None = None,
               version: str | None = None, method: str | None = None) -> list[SearchResult]:
        """Retrieve vector neighbors or fuse them with BM25, preserving provenance.

        Each hybrid branch supplies at most max(k, candidate_limit) candidates,
        after filtering. RRF truncates their union to k; raw scores are never
        added. last_search records all candidates, including those outside top k.
        Exact get()/rule() bypass this path and never build a lexical index.
        """
        method = self.method if method is None else method
        if method not in METHODS:
            raise ValueError(f"Unknown retrieval method: {method}")
        if type(k) is not int or k <= 0:
            raise ValueError("k must be a positive integer")
        if not query.strip():
            raise ValueError("Query must not be empty")
        where = _where(kind, version)
        lexical = self._lexical_index(version) if method == "hybrid" else None
        limit = max(k, self.candidate_limit) if method == "hybrid" else k
        self.last_search = {"method": method, "candidate_limit": limit, "candidates": []}
        count = self.collection.count()
        if count == 0:
            return []
        vectors = self.embedder.embed([query.strip()])
        validate_vectors(vectors, 1, self.config.dimensions)
        neighbors = _results(self.collection.query(
            query_embeddings=vectors, n_results=min(limit, count), where=where,
            include=["documents", "metadatas", "distances"]), vector=True)
        vector_ids = [result.id for result in neighbors]
        if method == "vector":
            results = [replace(result, retrieval_method=method, ranks={"vector": rank})
                       for rank, result in enumerate(neighbors, 1)]
            self.last_search["candidates"] = [
                {"id": result.id, "ranks": result.ranks} for result in results]
            return results
        lexical_ids = lexical.search(query, limit=limit, kind=kind)
        fused = reciprocal_rank_fusion({"vector": vector_ids, "bm25": lexical_ids})
        self.last_search["candidates"] = [
            {"id": identifier, "ranks": ranks, "fusion_score": score}
            for identifier, score, ranks in fused]
        by_id = {result.id: result for result in neighbors}
        missing = [identifier for identifier, _, _ in fused[:k] if identifier not in by_id]
        if missing:
            # Fetch lexical-only winners from Chroma, retaining one source contract.
            by_id.update((result.id, result) for result in _results(self.collection.get(
                ids=missing, include=["documents", "metadatas"])))
        return [replace(by_id[identifier], retrieval_method=method, ranks=ranks, fusion_score=score)
                for identifier, score, ranks in fused[:k]]

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
    mode.add_argument("query", nargs="?", help="Free-text retrieval query (the corpus is English)")
    mode.add_argument("--rule", help="Exact rule number, e.g. 702.49a; no embedding request")
    mode.add_argument("--id", help="Exact document ID; no embedding request")
    parser.add_argument("-k", type=int, default=DEFAULT_K, help=f"Positive number of search results (default: {DEFAULT_K})")
    parser.add_argument("--kind", choices=KINDS)
    parser.add_argument("--version", help="Filter by exact rulebook edition")
    parser.add_argument("--db", type=Path, default=Path("data/chroma"))
    parser.add_argument("--collection", default="magic-rules")
    parser.add_argument("--method", choices=METHODS, default=DEFAULT_METHOD)
    parser.add_argument("--source", type=Path, default=DEFAULT_INPUT, help="Matching JSONL for BM25")
    parser.add_argument("--candidate-limit", type=int, default=DEFAULT_CANDIDATES)
    args = parser.parse_args()
    if args.rule is not None and args.kind not in (None, "rule"):
        parser.error("--rule requires --kind rule or no kind filter")
    try:
        retriever = Retriever(args.db, args.collection, source_path=args.source,
                              method=args.method, candidate_limit=args.candidate_limit)
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
                      "search": retriever.last_search,
                      "results": [result.to_dict() for result in results]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
