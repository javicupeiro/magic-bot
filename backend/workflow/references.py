"""Build bounded rulebook context without recursive reference crawling.

Semantic search can return the same document for several queries. Keep each ID
once, retain complete records, then optionally follow stored reference IDs.
Expansion reads only the initial selected sources: A -> B -> C adds B from A,
but never follows newly added B to C. Existing retrieval/edition rules are reused.
"""

import json

from backend.retrieval.retrieval import SearchResult


def context_size(sources: list[SearchResult]) -> int:
    """Measure the JSON rules array exactly as it will be serialized for generation.

    Metadata, punctuation and text all count. The caller reserves the surrounding
    evidence object and verified-card data separately. This is not token counting.
    """
    return len(json.dumps([source.to_dict() for source in sources], ensure_ascii=False))


def bounded_context(sources: list[SearchResult], budget: int) -> list[SearchResult]:
    """Deduplicate by ID and select whole documents without exceeding budget.

    Input order preserves retrieval priority. A large document is skipped rather
    than truncated; later smaller documents may still fit. A dictionary provides
    both ID membership checks and deterministic insertion order.
    """
    selected = {}
    for source in sources:
        if source.id not in selected and context_size([*selected.values(), source]) <= budget:
            selected[source.id] = source
    return list(selected.values())


def expand_references(sources, retriever, *, budget=24000, max_extra=6):
    """Read references from initial documents only, preserving each source edition.

    A snapshot prevents recursion through cycles. IDs deduplicate both retrieved
    and exact records; no more than six additional documents can be included.
    """
    if not 0 <= max_extra <= 6:
        raise ValueError("max_extra must be between zero and six")
    # 1. Freeze the initial sources. Never append to this traversal collection.
    initial = bounded_context(sources, budget)
    selected = {source.id: source for source in initial}
    # Track lookups separately from accepted documents: even a missing/oversized
    # reference should not trigger the same exact lookup repeatedly in an edition.
    requested = set()
    extra = 0
    # 2. Follow parser-resolved IDs in the referring document's edition.
    for source in initial:
        for reference in source.references:
            if not reference.resolved:
                continue
            key = (reference.target_id, source.version)
            if key in requested:
                continue
            requested.add(key)
            if extra >= max_extra:
                return list(selected.values())
            # 3. Reuse exact storage lookup; no vector embedding is needed here.
            for target in retriever.get(reference.target_id, version=source.version):
                if target.id in selected:
                    continue
                # Count only newly included documents toward the six-document cap.
                if extra < max_extra and context_size([*selected.values(), target]) <= budget:
                    selected[target.id] = target
                    extra += 1
    return list(selected.values())
