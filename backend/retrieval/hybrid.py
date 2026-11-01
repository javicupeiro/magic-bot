"""Provide in-memory BM25 and rank fusion for Retriever, without storage or HTTP.

The lexical index reads the same parsed documents as vector ingestion. Only
positions enter fusion: BM25 scores and cosine distances have different scales.
"""

from collections import Counter, defaultdict
import math
import re

from backend.ingestion.documents import Document
from backend.ingestion.ingest import semantic_text

# Match rule numbers before words so 702.49a stays one token. Unicode words keep
# Spanish queries usable; no stemming, translation or typo correction is implied.
TOKEN_PATTERN = re.compile(r"\d+(?:\.\d+)+[a-z]?|[^\W_]+", re.IGNORECASE)
RRF_CONSTANT = 60
DEFAULT_CANDIDATES = 20


def tokenize(text: str) -> list[str]:
    """Case-fold words and preserve dotted rule identifiers as complete tokens."""
    return TOKEN_PATTERN.findall(text.casefold())


class BM25Index:
    """Score positive lexical matches with BM25 (k1=1.5, b=0.75).

    Postings and document lengths live only in memory. Retriever owns corpus
    validation against Chroma before constructing this index, once per edition.
    """

    def __init__(self, documents: list[Document]):
        """Tokenize title/body and rule number, retaining original records by ID."""
        self.documents = {document.id: document for document in documents}
        self.postings = defaultdict(dict)
        self.lengths = {}
        for document in documents:
            # The parser removes rule numbers from bodies; add them explicitly.
            text = f"{document.rule_number or ''}\n{semantic_text(document)}"
            terms = Counter(tokenize(text))
            self.lengths[document.id] = sum(terms.values())
            for term, frequency in terms.items():
                self.postings[term][document.id] = frequency
        self.average_length = sum(self.lengths.values()) / max(1, len(documents))

    def search(self, query: str, *, limit: int, kind: str | None = None) -> list[str]:
        """Return at most limit IDs, filtering kind before truncation.

        IDF uses the complete selected edition, including all source kinds.
        Nonmatches are excluded. Equal scores sort by ID for reproducibility.
        """
        scores = defaultdict(float)
        count = len(self.documents)
        for term in sorted(set(tokenize(query))):
            postings = self.postings.get(term, {})
            inverse_frequency = math.log(1 + (count - len(postings) + 0.5) / (len(postings) + 0.5))
            for identifier, frequency in postings.items():
                if kind is not None and self.documents[identifier].kind != kind:
                    continue
                length_ratio = self.lengths[identifier] / self.average_length
                denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * length_ratio)
                scores[identifier] += inverse_frequency * frequency * 2.5 / denominator
        return sorted(scores, key=lambda identifier: (-scores[identifier], identifier))[:limit]


def reciprocal_rank_fusion(rankings: dict[str, list[str]]) -> list[tuple[str, float, dict[str, int]]]:
    """Deduplicate IDs and fuse one-based ranks with sum(1 / (60 + rank)).

    A repeated ID contributes only its first position within each method. Missing
    IDs contribute zero. Ties sort by ID, independent of dictionary/input order.
    Scores describe agreement in these candidate lists, not answer confidence.
    """
    positions = defaultdict(dict)
    for method, identifiers in rankings.items():
        for rank, identifier in enumerate(identifiers, 1):
            positions[identifier].setdefault(method, rank)
    fused = [(identifier, sum(1 / (RRF_CONSTANT + rank) for rank in ranks.values()), ranks)
             for identifier, ranks in positions.items()]
    return sorted(fused, key=lambda candidate: (-candidate[1], candidate[0]))
