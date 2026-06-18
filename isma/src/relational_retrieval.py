"""
Relational Retrieval — Bi-Substrate Cascading Membrane.

Dream Cycle architecture (5/5 unanimous, March 2026):
  State Alpha: Neo4j motif intersection (both concepts map to motifs)
  State Beta:  Graph-filtered vector search (one motif known, one text)
  State Gamma: Decomposed dual retrieval + harmonic mean scoring

Cascade: Alpha → Beta → Gamma, threshold K=5. First stage with ≥K results wins.
Relational-specific reranker prompt on all paths.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as dc_replace
from typing import Any, Dict, List, Optional, Tuple

from isma.src.hmm.motifs import V0_MOTIFS
from isma.src.query_classifier import MOTIF_KEYWORDS
from isma.src.retrieval import (
    WEAVIATE_URL,
    SearchResult,
    TileResult,
    _get_embedding,
    _get_neo4j,
)

log = logging.getLogger(__name__)

# Minimum results before falling through to next cascade stage
CASCADE_K = 5

# ── Concept → Motif Resolution ────────────────────────────────────

# Build a lookup: lowercase phrase → HMM.MOTIF_ID
_CONCEPT_TO_MOTIF: Dict[str, str] = {}
for _kw, _mid in MOTIF_KEYWORDS.items():
    _CONCEPT_TO_MOTIF[_kw.lower()] = _mid
    # Also map singular/plural variants
    if _kw.endswith("s"):
        _CONCEPT_TO_MOTIF[_kw[:-1].lower()] = _mid
    else:
        _CONCEPT_TO_MOTIF[(_kw + "s").lower()] = _mid


def resolve_concept_to_motif(concept: str) -> Optional[str]:
    """Map a concept phrase to an HMM motif ID.

    Tries exact match, then substring match, then word-overlap matching.
    Returns None if no motif can be resolved.
    """
    c = concept.lower().strip()

    # Exact match
    if c in _CONCEPT_TO_MOTIF:
        return _CONCEPT_TO_MOTIF[c]

    # Substring: concept contains a motif keyword (e.g., "bristle signals" contains "bristle signal")
    best_match = None
    best_len = 0
    for keyword, motif_id in _CONCEPT_TO_MOTIF.items():
        if keyword in c and len(keyword) > best_len:
            best_match = motif_id
            best_len = len(keyword)
    if best_match:
        return best_match

    # Word overlap: any significant word from concept appears in a motif keyword
    # (e.g., "infrastructure milestones" matches "technical infrastructure")
    concept_words = set(c.split()) - {"the", "a", "an", "and", "of", "to", "in", "for", "with"}
    best_overlap = 0
    for keyword, motif_id in _CONCEPT_TO_MOTIF.items():
        kw_words = set(keyword.split())
        overlap = len(concept_words & kw_words)
        if overlap > best_overlap and overlap > 0:
            best_overlap = overlap
            best_match = motif_id

    return best_match


# ── State Alpha: Neo4j Motif Intersection ─────────────────────────

def _is_background_motif(motif_id: str) -> bool:
    """Check if a motif is marked as background (too broad for intersection)."""
    m = V0_MOTIFS.get(motif_id)
    return m.background if m else False


def _state_alpha(motif_a: str, motif_b: str) -> List[str]:
    """Find content_hashes where tile EXPRESSES both motifs.

    Uses Neo4j indexed lookup — deterministic, O(1) with indexes.
    Returns ALL matching hashes (no arbitrary LIMIT — let downstream
    RRF + reranker handle ranking).

    Skips pairs where either motif is marked as background (v0.3.0) —
    background motifs match too many tiles, drowning out meaningful intersections.
    """
    if _is_background_motif(motif_a) or _is_background_motif(motif_b):
        log.info(
            "State Alpha: skipping background motif pair %s ∩ %s",
            motif_a, motif_b,
        )
        return []

    try:
        driver = _get_neo4j()
    except Exception as e:
        log.warning("Neo4j unavailable for State Alpha: %s", e)
        return []

    query = """
        MATCH (t:HMMTile)-[:EXPRESSES]->(m1:HMMMotif {motif_id: $motif_a}),
              (t)-[:EXPRESSES]->(m2:HMMMotif {motif_id: $motif_b})
        RETURN DISTINCT t.content_hash AS content_hash
        LIMIT 500
    """
    try:
        with driver.session() as session:
            result = session.run(query, motif_a=motif_a, motif_b=motif_b)
            hashes = [r["content_hash"] for r in result if r["content_hash"]]
        log.info(
            "State Alpha: %s ∩ %s → %d tiles",
            motif_a, motif_b, len(hashes),
        )
        return hashes
    except Exception as e:
        log.warning("State Alpha query failed: %s", e)
        return []


# ── State Beta: Graph-Filtered Vector Search ──────────────────────

def _state_beta(
    known_motif: str,
    unknown_concept: str,
    top_k: int = 50,
) -> List[Tuple[str, float]]:
    """Asymmetric bridge: get tile IDs expressing known motif from Neo4j,
    then search for unknown concept WITHIN that set via Weaviate.

    Returns list of (content_hash, similarity_score).
    """
    # Step 1: Get tile content_hashes expressing the known motif
    try:
        driver = _get_neo4j()
        with driver.session() as session:
            result = session.run(
                """
                MATCH (t:HMMTile)-[:EXPRESSES]->(m:HMMMotif {motif_id: $motif})
                RETURN DISTINCT t.content_hash AS content_hash
                LIMIT 500
                """,
                motif=known_motif,
            )
            motif_hashes = [r["content_hash"] for r in result if r["content_hash"]]
    except Exception as e:
        log.warning("State Beta Neo4j lookup failed: %s", e)
        return []

    if not motif_hashes:
        return []

    # Step 2: Embed the unknown concept
    vector = _get_embedding(unknown_concept)
    if not vector:
        return []

    # Step 3: Weaviate nearVector search filtered to motif tile set
    import json as _json
    import requests

    # Build OR-filter for content_hash containment
    if len(motif_hashes) == 1:
        where_clause = (
            f'where: {{ path: ["content_hash"], operator: Equal, '
            f'valueText: "{motif_hashes[0]}" }}'
        )
    else:
        # Limit to first 200 hashes to keep GQL manageable
        batch = motif_hashes[:200]
        operands = ", ".join(
            f'{{ path: ["content_hash"], operator: Equal, valueText: "{h}" }}'
            for h in batch
        )
        where_clause = f'where: {{ operator: Or, operands: [{operands}] }}'

    gql = (
        f'{{ Get {{ ISMA_Quantum('
        f'nearVector: {{ vector: {_json.dumps(vector)} }}'
        f' {where_clause}'
        f' limit: {top_k}'
        f') {{ content_hash _additional {{ distance }} }} }} }}'
    )

    try:
        r = requests.post(
            f"{WEAVIATE_URL}/v1/graphql",
            json={"query": gql},
            timeout=15,
        )
        data = r.json()
        tiles = data.get("data", {}).get("Get", {}).get("ISMA_Quantum", [])
        results = []
        for t in tiles:
            ch = t.get("content_hash", "")
            dist = t.get("_additional", {}).get("distance", 1.0)
            score = 1.0 - dist  # Convert distance to similarity
            if ch:
                results.append((ch, score))

        # Deduplicate by content_hash (Weaviate may return multiple tiles per hash)
        seen = set()
        deduped = []
        for ch, score in results:
            if ch not in seen:
                seen.add(ch)
                deduped.append((ch, score))

        log.info(
            "State Beta: %s + '%s' → %d motif tiles, %d vector matches",
            known_motif, unknown_concept, len(motif_hashes), len(deduped),
        )
        return deduped
    except Exception as e:
        log.warning("State Beta Weaviate search failed: %s", e)
        return []


# ── State Gamma: Decomposed Dual Retrieval + Harmonic Mean ────────

def _state_gamma(
    concept_a: str,
    concept_b: str,
    top_k: int = 50,
) -> List[Tuple[str, float]]:
    """Two independent embedding searches, scored by harmonic mean.

    Score = 2 * SimA * SimB / (SimA + SimB)
    Penalizes imbalance — surfaces tiles relevant to BOTH concepts.
    """
    import json as _json
    import requests

    # Embed both concepts in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_get_embedding, concept_a)
        fut_b = pool.submit(_get_embedding, concept_b)
        vec_a = fut_a.result(timeout=10)
        vec_b = fut_b.result(timeout=10)

    if not vec_a or not vec_b:
        return []

    # Run both nearVector searches in parallel
    def _search_vec(vector, label):
        gql = (
            f'{{ Get {{ ISMA_Quantum('
            f'nearVector: {{ vector: {_json.dumps(vector)} }}'
            f' limit: {top_k}'
            f') {{ content_hash _additional {{ distance }} }} }} }}'
        )
        try:
            r = requests.post(
                f"{WEAVIATE_URL}/v1/graphql",
                json={"query": gql},
                timeout=15,
            )
            data = r.json()
            tiles = data.get("data", {}).get("Get", {}).get("ISMA_Quantum", [])
            results = {}
            for t in tiles:
                ch = t.get("content_hash", "")
                dist = t.get("_additional", {}).get("distance", 1.0)
                if ch and ch not in results:
                    results[ch] = 1.0 - dist
            return results
        except Exception as e:
            log.warning("State Gamma %s search failed: %s", label, e)
            return {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_search_vec, vec_a, "concept_A")
        fut_b = pool.submit(_search_vec, vec_b, "concept_B")
        scores_a = fut_a.result(timeout=20)
        scores_b = fut_b.result(timeout=20)

    if not scores_a or not scores_b:
        return []

    # Compute harmonic mean for dual-relevant tiles, with one-sided fallback
    all_hashes = set(scores_a.keys()) | set(scores_b.keys())
    scored = []
    for ch in all_hashes:
        sim_a = scores_a.get(ch, 0.0)
        sim_b = scores_b.get(ch, 0.0)
        if sim_a > 0.01 and sim_b > 0.01:
            # Both concepts present — full harmonic mean
            harmonic = 2.0 * sim_a * sim_b / (sim_a + sim_b)
            scored.append((ch, harmonic))
        elif sim_a > 0.3 or sim_b > 0.3:
            # One-sided strong match — reduced score (reranker decides relevance)
            scored.append((ch, max(sim_a, sim_b) * 0.3))

    # Sort by harmonic mean score descending
    scored.sort(key=lambda x: x[1], reverse=True)
    log.info(
        "State Gamma: '%s' (%d hits) + '%s' (%d hits) → %d dual-relevant tiles",
        concept_a, len(scores_a), concept_b, len(scores_b), len(scored),
    )
    return scored


# ── Cascade Orchestrator ──────────────────────────────────────────

def search_relational(
    query: str,
    sub_queries: List[str],
    top_k: int = 10,
) -> Dict[str, Any]:
    """Parallel relational retrieval: Alpha + Gamma merged via RRF.

    Runs structural (Alpha/Beta) and semantic (Gamma) paths in parallel,
    merges via RRF. Alpha tiles get 2x weight (structural certainty),
    Gamma tiles get 1x weight (semantic similarity). This ensures gold tiles
    that are structurally annotated with different-but-related motifs still
    surface via Gamma's harmonic mean scoring.

    Args:
        query: Original relational query
        sub_queries: [concept_a, concept_b, full_query] from _decompose_relational
        top_k: Number of results to return

    Returns:
        Dict with content_hashes, scores, and cascade diagnostics.
    """
    t0 = time.monotonic()

    # Extract concepts
    if len(sub_queries) >= 2:
        concept_a = sub_queries[0]
        concept_b = sub_queries[1]
    else:
        # Can't decompose — no relational enhancement possible
        return {
            "hashes": [],
            "scores": {},
            "state": "none",
            "diagnostics": {"reason": "fewer than 2 concepts extracted"},
        }

    # Resolve concepts to motifs
    motif_a = resolve_concept_to_motif(concept_a)
    motif_b = resolve_concept_to_motif(concept_b)

    # Run structural + semantic paths in parallel
    k_rrf = 60
    rrf_scores: Dict[str, float] = {}
    states_used = []

    def _run_structural():
        """Alpha or Beta — graph-based structural retrieval."""
        if motif_a and motif_b:
            return ("alpha", _state_alpha(motif_a, motif_b))
        elif motif_a and not motif_b:
            results = _state_beta(motif_a, concept_b, top_k=top_k * 5)
            return ("beta", [h for h, _ in results])
        elif motif_b and not motif_a:
            results = _state_beta(motif_b, concept_a, top_k=top_k * 5)
            return ("beta", [h for h, _ in results])
        return ("none", [])

    def _run_semantic():
        """Gamma — harmonic mean of dual embedding search."""
        return _state_gamma(concept_a, concept_b, top_k=top_k * 5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        structural_fut = pool.submit(_run_structural)
        semantic_fut = pool.submit(_run_semantic)

        # Collect structural results (2x RRF weight)
        try:
            struct_state, struct_hashes = structural_fut.result(timeout=20)
            if struct_hashes:
                states_used.append(struct_state)
                for rank, ch in enumerate(struct_hashes):
                    rrf_scores[ch] = rrf_scores.get(ch, 0) + 2.0 / (k_rrf + rank + 1)
        except Exception as e:
            log.warning("Structural path failed: %s", e)

        # Collect semantic results (1x RRF weight)
        try:
            gamma_results = semantic_fut.result(timeout=25)
            if gamma_results:
                states_used.append("gamma")
                for rank, (ch, _score) in enumerate(gamma_results):
                    rrf_scores[ch] = rrf_scores.get(ch, 0) + 1.0 / (k_rrf + rank + 1)
        except Exception as e:
            log.warning("Semantic path failed: %s", e)

    # Sort by combined RRF score
    sorted_hashes = sorted(rrf_scores, key=lambda ch: rrf_scores[ch], reverse=True)
    state = "+".join(states_used) if states_used else "none"

    elapsed_ms = (time.monotonic() - t0) * 1000
    log.info(
        "Relational cascade: state=%s, %d results in %.0fms "
        "(concepts: '%s' [%s] + '%s' [%s])",
        state, len(sorted_hashes), elapsed_ms,
        concept_a, motif_a or "unmapped",
        concept_b, motif_b or "unmapped",
    )

    return {
        "hashes": sorted_hashes,  # RRF-ranked — downstream reranker refines
        "scores": rrf_scores,
        "state": state,
        "diagnostics": {
            "concept_a": concept_a,
            "concept_b": concept_b,
            "motif_a": motif_a,
            "motif_b": motif_b,
            "state": state,
            "result_count": len(sorted_hashes),
            "elapsed_ms": round(elapsed_ms, 1),
        },
    }
