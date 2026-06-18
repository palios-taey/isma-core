"""
Provenance-Weighted Retrieval Scorer (Phase 8B)

Replaces flat coherence_boost with full epistemic scoring.
Reranks candidates based on provenance, graph support, correction
obedience, source independence, and echo penalties.

"Relevance decides whether a tile is about the question.
 Provenance decides whether a tile deserves to shape the answer."

Usage:
    from isma.src.provenance_scorer import apply_provenance_scoring
    tiles = apply_provenance_scoring(tiles, query_type="canon")
"""

import logging
import math
import os
from collections import Counter, defaultdict
from dataclasses import replace as dc_replace
from typing import Dict, List, Optional, Tuple

try:
    from neo4j import GraphDatabase
    _NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    _neo4j_driver = None

    def _get_neo4j():
        global _neo4j_driver
        if _neo4j_driver is None:
            _neo4j_driver = GraphDatabase.driver(_NEO4J_URI)
        return _neo4j_driver
except ImportError:
    _get_neo4j = None

log = logging.getLogger(__name__)

# ============================================================================
# Truth Tier Priors
# ============================================================================

TIER_PRIORS = {
    "constitutional": 1.00,
    "verified": 0.85,
    "derived": 0.65,
    "raw": 0.45,
    "retracted": 0.00,
}

AUTHORITY_PRIORS = {
    "binding": 1.00,
    "verified": 0.85,
    "advisory": 0.70,
    "exploratory": 0.50,
}

SOURCE_TYPE_PRIORS = {
    "kernel": 1.00,
    "layer0": 0.95,
    "layer1": 0.90,
    "corpus": 0.80,
    "document": 0.75,
    "transcript": 0.60,
    "exchange": 0.50,
}

# ============================================================================
# Memory Zone Priors (by query mode)
# ============================================================================

ZONE_PRIORS = {
    "canon": {"canon": 1.00, "sandbox": 0.25, "retracted": 0.00},
    "research": {"canon": 1.00, "sandbox": 0.70, "retracted": 0.10},
    "audit": {"canon": 0.95, "sandbox": 0.95, "retracted": 0.80},
    "exploration": {"canon": 0.85, "sandbox": 1.00, "retracted": 0.00},
    "consent": {"canon": 1.00, "sandbox": 0.30, "retracted": 0.00},
    # Fallback for standard query types
    "exact": {"canon": 1.00, "sandbox": 0.40, "retracted": 0.00},
    "temporal": {"canon": 1.00, "sandbox": 0.60, "retracted": 0.10},
    "conceptual": {"canon": 0.90, "sandbox": 0.80, "retracted": 0.00},
    "relational": {"canon": 1.00, "sandbox": 0.50, "retracted": 0.00},
    "memory": {"canon": 0.95, "sandbox": 1.00, "retracted": 0.00},
    "humor": {"canon": 0.85, "sandbox": 1.00, "retracted": 0.00},
    "motif": {"canon": 1.00, "sandbox": 0.60, "retracted": 0.00},
    "default": {"canon": 1.00, "sandbox": 0.50, "retracted": 0.00},
}

QUERY_TYPE_TO_MODE = {
    "exact": "canon",
    "motif": "canon",
    "temporal": "research",
    "relational": "research",
    "conceptual": "exploration",
    "memory": "exploration",
    "humor": "exploration",
    "default": "default",
}

# ============================================================================
# Scoring Weights
# ============================================================================

# FinalScore components — all signals wired (Phase 8C complete)
W_HYBRID = 0.34
W_RERANK = 0.18
W_PROVENANCE = 0.18
W_GRAPH = 0.13       # Neo4j neighbor vote — boosted from 0.10 per Dream Cycle consensus
W_CORRECTION = 0.05  # Binary signal, gate not gradient — reduced from 0.07
W_INDEPENDENCE = 0.05 # 1/sqrt(n) already has diminishing returns — reduced from 0.06
W_TEMPORAL = 0.04    # Freshness decay (stronger for temporal queries)
W_MODE_FIT = 0.03    # Source-type fit to query mode
P_RETRACTION = 0.16
P_CONTRADICTION = 0.10
P_ECHO = 0.08

# Phase 9: Mode-specific weight deltas (ChatGPT + Claude consensus)
# Soft deltas around proven global weights. Keep global as baseline.
# Positive delta = boost that signal for this query mode.
# Deltas must sum to 0 per mode (zero-sum redistribution).
MODE_WEIGHT_DELTAS = {
    "relational": {
        "graph": +0.04,         # Relational queries need more graph signal
        "independence": +0.02,  # Cross-source diversity matters
        "mode_fit": +0.02,     # Source type relevance
        "hybrid": -0.05,       # Less reliance on raw relevance
        "rerank": -0.03,       # Reranker less useful for multi-hop
    },
    "exact": {
        "hybrid": +0.04,       # Lexical matching is key for exact
        "provenance": +0.02,   # Authority matters for facts
        "correction": +0.02,   # Corrections critical for factual
        "graph": -0.05,        # Graph less relevant for point lookups
        "temporal": -0.03,     # Time less relevant for facts
    },
    "temporal": {
        "temporal": +0.06,     # Freshness is the whole point
        "correction": +0.02,   # Corrections update temporal claims
        "graph": -0.04,        # Graph less relevant for time queries
        "mode_fit": -0.04,     # Mode fit less important
    },
    "conceptual": {
        "rerank": +0.04,       # Reranker best at semantic matching
        "provenance": +0.02,   # Authority matters for concepts
        "independence": +0.02, # Diverse sources strengthen concepts
        "hybrid": -0.04,       # Less lexical, more semantic
        "graph": -0.04,        # Graph less helpful for broad concepts
    },
    "memory": {
        "rerank": +0.06,       # Conversational recall benefits from stronger reranking
        "hybrid": +0.02,       # Preserve lexical cues from prior turns
        "mode_fit": +0.02,     # Favor conversational source types
        "graph": -0.05,        # Graph adds little to recall requests
        "temporal": -0.05,     # Recency matters less than matching the right exchange
    },
    "humor": {
        "rerank": +0.06,       # Tone matching depends on reranker semantics
        "hybrid": +0.02,       # Lexical jokes/phrases still matter
        "mode_fit": +0.02,     # Favor conversational content
        "graph": -0.05,        # Graph adds little to humorous passages
        "provenance": -0.05,   # Authority matters less than conversational fit
    },
    "motif": {
        "graph": +0.05,        # Motif queries benefit from graph traversal
        "mode_fit": +0.03,     # Source type relevance for motif domains
        "hybrid": -0.05,       # Less reliance on keyword matching
        "temporal": -0.03,     # Time less relevant for motif patterns
    },
}

# Weight key mapping for delta application
_WEIGHT_KEYS = {
    "hybrid": "W_HYBRID",
    "rerank": "W_RERANK",
    "provenance": "W_PROVENANCE",
    "graph": "W_GRAPH",
    "correction": "W_CORRECTION",
    "independence": "W_INDEPENDENCE",
    "temporal": "W_TEMPORAL",
    "mode_fit": "W_MODE_FIT",
}


def get_mode_weights(query_type: str) -> dict:
    """Get effective weights for a query type, applying mode-specific deltas."""
    base = {
        "hybrid": W_HYBRID + W_RERANK,  # Combined as raw relevance
        "provenance": W_PROVENANCE,
        "graph": W_GRAPH,
        "correction": W_CORRECTION,
        "independence": W_INDEPENDENCE,
        "temporal": W_TEMPORAL,
        "mode_fit": W_MODE_FIT,
    }
    deltas = MODE_WEIGHT_DELTAS.get(query_type, {})
    effective = {}
    for key, val in base.items():
        delta = deltas.get(key, 0.0)
        effective[key] = max(0.0, val + delta)
    return effective

# ProvenancePrior sub-weights
PP_TIER = 0.30
PP_AUTHORITY = 0.20
PP_ZONE = 0.18
PP_SOURCE_TYPE = 0.12
PP_LINEAGE = 0.10
PP_PROMOTION = 0.10


# ============================================================================
# Provenance Prior
# ============================================================================

def provenance_prior(tile, query_mode: str) -> float:
    """Compute ProvenancePrior(d, mode) from tile metadata."""
    # TierPrior
    truth_tier = getattr(tile, "truth_tier", None) or "raw"
    tier_score = TIER_PRIORS.get(truth_tier, 0.45)

    # AuthorityPrior
    authority = getattr(tile, "authority", None) or "exploratory"
    auth_score = AUTHORITY_PRIORS.get(authority, 0.50)

    # ZonePrior
    memory_zone = getattr(tile, "memory_zone", None) or "sandbox"
    zone_map = ZONE_PRIORS.get(query_mode, ZONE_PRIORS["default"])
    zone_score = zone_map.get(memory_zone, 0.50)

    # SourceTypePrior
    source_type = getattr(tile, "source_type", None) or "transcript"
    source_score = SOURCE_TYPE_PRIORS.get(source_type, 0.50)

    # LineageIntegrity — has lineage_root and it's not self
    lineage_root = getattr(tile, "lineage_root", None)
    lineage_score = 0.7 if lineage_root else 0.3

    # PromotionState
    promotion = getattr(tile, "promotion_state", None) or "unpromoted"
    if promotion == "promoted":
        promo_score = 1.0
    elif promotion == "candidate":
        promo_score = 0.7
    else:
        promo_score = 0.4

    base = (
        PP_TIER * tier_score
        + PP_AUTHORITY * auth_score
        + PP_ZONE * zone_score
        + PP_SOURCE_TYPE * source_score
        + PP_LINEAGE * lineage_score
        + PP_PROMOTION * promo_score
    )

    # Coherence quality boost (from Phase 7 LinBP truth_coherence_score).
    # Max S ~0.75 from full-graph BP. Scale to give up to 5% additive boost.
    coherence_score = getattr(tile, "truth_coherence_score", 0.0) or 0.0
    coherence_boost = 0.05 * min(coherence_score / 0.75, 1.0)

    return min(base + coherence_boost, 1.0)  # Clamp to [0,1] — coherence_boost can push above 1.0


# ============================================================================
# Correction Obedience
# ============================================================================

def correction_obedience(tile) -> float:
    """Score based on whether tile respects correction chain.

    If tile is superseded, it should rank lower.
    If tile is the corrector, it should rank higher.
    """
    correction_status = getattr(tile, "correction_status", None)
    superseded_by = getattr(tile, "superseded_by", None)

    if correction_status == "corrected" or superseded_by:
        return 0.0  # This tile has been superseded
    elif correction_status == "corrector":
        return 1.0  # This tile IS the correction
    elif correction_status == "current":
        return 0.9  # Explicitly marked current
    else:
        return 0.5  # Unknown — neutral


# ============================================================================
# Source Independence & Echo Penalty
# ============================================================================

def compute_echo_penalties(tiles) -> Dict[str, float]:
    """Compute per-tile echo penalty based on session/source clustering.

    Penalizes repeated parent documents, not overlapping phi-tiles.
    """
    # Count unique parent tiles per session/source cluster.
    # This avoids penalizing overlapping chunks from the same thought.
    session_counts = Counter()
    source_counts = Counter()
    seen_session_parents = set()
    seen_source_parents = set()

    for tile in tiles:
        parent = getattr(tile, "parent_tile_id", None) or getattr(tile, "content_hash", None) or str(id(tile))
        sc = getattr(tile, "session_cluster_id", None) or getattr(tile, "session_id", None) or "unknown"
        src = getattr(tile, "source_cluster_id", None) or getattr(tile, "source_file", None) or "unknown"

        session_parent_key = (sc, parent)
        source_parent_key = (src, parent)

        if session_parent_key not in seen_session_parents:
            session_counts[sc] += 1
            seen_session_parents.add(session_parent_key)

        if source_parent_key not in seen_source_parents:
            source_counts[src] += 1
            seen_source_parents.add(source_parent_key)

    penalties = {}
    for tile in tiles:
        key = getattr(tile, "content_hash", None) or id(tile)
        sc = getattr(tile, "session_cluster_id", None) or getattr(tile, "session_id", None) or "unknown"
        src = getattr(tile, "source_cluster_id", None) or getattr(tile, "source_file", None) or "unknown"

        # Thresholds raised to respect phi-tiling overlap.
        session_penalty = max(0, (session_counts[sc] - 3) * 0.15)
        source_penalty = max(0, (source_counts[src] - 4) * 0.10)

        penalties[key] = min(session_penalty + source_penalty, 1.0)

    return penalties


def source_independence(tile, source_counts: Dict[str, int]) -> float:
    """Score tile independence — tiles from rare sources get higher scores."""
    src = getattr(tile, "source_cluster_id", None) or getattr(tile, "source_file", None) or "unknown"
    count = source_counts.get(src, 1)
    return 1.0 / math.sqrt(max(count, 1))


# ============================================================================
# Retraction & Contradiction Penalties
# ============================================================================

def retraction_penalty(tile, query_mode: str = "default") -> float:
    """Penalty for retracted tiles."""
    if query_mode == "audit":
        return 0.0

    truth_tier = getattr(tile, "truth_tier", None)
    if truth_tier == "retracted":
        return 1.0
    correction_status = getattr(tile, "correction_status", None)
    if correction_status == "retracted":
        return 1.0
    return 0.0


def contradiction_penalty(tile) -> float:
    """Penalty for tiles with explicit contradiction signals."""
    has_contradictions = getattr(tile, "has_contradictions", False)
    contradiction_count = getattr(tile, "contradiction_count", 0) or 0

    if has_contradictions or contradiction_count > 0:
        if contradiction_count <= 0:
            contradiction_count = 1
        return min(1.0, float(contradiction_count) * 0.3)
    return 0.0


# ============================================================================
# Graph Support (Neo4j Neighbor Vote)
# ============================================================================

_GRAPH_SUPPORT_MAX_CANDIDATES = 10  # Limit candidates to keep query fast


def compute_graph_support(tiles) -> Dict[str, float]:
    """Score tiles by pre-materialized graph support edges.

    Uses supports_topk edges (weighted by embedding similarity) and
    challenges_topk edges (contradiction penalty) for fast graph scoring.
    Falls back to typed edge vote count if supports_topk edges don't exist.

    Returns normalized score in [0, 1]. Falls back to 0.0 if Neo4j unavailable.
    Limited to top-10 candidates. Uses transaction timeout (2s) as guardrail.
    """
    if _get_neo4j is None:
        return {}

    hashes = [getattr(t, "content_hash", None) for t in tiles]
    hashes = [h for h in hashes if h]
    if len(hashes) < 2:
        return {}

    # Limit to top candidates to keep query tractable
    hashes = list(dict.fromkeys(hashes))[:_GRAPH_SUPPORT_MAX_CANDIDATES]

    import threading

    result_box: List[Dict] = [{}]
    error_box: List[Optional[Exception]] = [None]

    def _run_query():
        try:
            driver = _get_neo4j()
            with driver.session() as session:
                result = session.run(
                    """
                    WITH $hashes AS candidates
                    UNWIND candidates AS ch
                    OPTIONAL MATCH (t:HMMTile {content_hash: ch})-[r:RELATES_TO]-(n:HMMTile)
                    WHERE r.type IN ['supports_topk', 'motif_cooccurrence', 'references',
                                     'builds_on', 'extends']
                      AND n.content_hash IN $hashes AND n.content_hash <> ch
                    WITH ch, collect({type: r.type, weight: coalesce(r.weight, 1.0)}) as edges
                    WITH ch,
                         reduce(s = 0.0, e IN edges |
                           s + CASE WHEN e.type = 'supports_topk' THEN e.weight ELSE 1.0 END
                         ) as support_score
                    OPTIONAL MATCH (t2:HMMTile {content_hash: ch})-[c:RELATES_TO {type: 'challenges_topk'}]-(m:HMMTile)
                    WHERE m.content_hash IN $hashes AND m.content_hash <> ch
                    WITH ch, support_score, count(c) as challenge_count
                    RETURN ch as hash,
                           support_score - (challenge_count * 0.5) as score
                    """,
                    hashes=hashes,
                )
                result_box[0] = {rec["hash"]: rec["score"] for rec in result}
        except Exception as e:
            error_box[0] = e

    t = threading.Thread(target=_run_query, daemon=True)
    t.start()
    t.join(timeout=2.0)

    if t.is_alive():
        log.warning("Graph support query timed out (2s)")
        return {}

    if error_box[0]:
        log.warning("Graph support query failed: %s", error_box[0])
        return {}

    scores = result_box[0]
    if not scores:
        return {}

    # Normalize to [0, 1]
    max_score = max(scores.values()) or 1
    min_score = min(scores.values())
    score_range = max_score - min_score if max_score > min_score else 1
    return {h: max(0, (v - min_score) / score_range) for h, v in scores.items()}


# ============================================================================
# Temporal Freshness
# ============================================================================

# Half-life in days — how quickly freshness decays
_TEMPORAL_HALF_LIFE_DAYS = 90  # 3 months

# Temporal queries get stronger freshness boost
_TEMPORAL_QUERY_BOOST = 2.0


def compute_temporal_freshness(tile, query_type: str) -> float:
    """Compute temporal freshness score in [0, 1].

    Uses exponential decay based on tile timestamp. More recent tiles
    score higher. Temporal queries amplify the freshness signal.

    Returns 0.5 (neutral) if timestamp is missing or unparseable.
    """
    from datetime import datetime, timezone

    ts_str = getattr(tile, "timestamp", None)
    if not ts_str:
        return 0.5

    try:
        # Handle various timestamp formats
        ts_str = ts_str.rstrip("Z")
        if "T" in ts_str:
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        else:
            return 0.5
    except (ValueError, TypeError):
        return 0.5

    now = datetime.now(timezone.utc)
    age_days = max(0, (now - ts).total_seconds() / 86400)

    # Exponential decay: score = exp(-lambda * age)
    decay = math.exp(-math.log(2) / _TEMPORAL_HALF_LIFE_DAYS * age_days)

    # Temporal queries amplify the signal (steeper decay)
    if query_type == "temporal":
        decay = decay ** _TEMPORAL_QUERY_BOOST

    return decay


# ============================================================================
# Mode Fit
# ============================================================================

# Which source_types are preferred for each query_mode
_MODE_FIT_MAP = {
    "canon": {"corpus": 1.0, "kernel": 1.0, "declaration": 0.9, "transcript": 0.5},
    "research": {"transcript": 0.8, "corpus": 0.7, "exchange": 0.9, "kernel": 0.6},
    "audit": {"transcript": 1.0, "exchange": 1.0, "corpus": 0.7, "kernel": 0.5},
    "exploration": {"transcript": 0.7, "corpus": 0.7, "exchange": 0.7, "kernel": 0.7},
    "default": {"corpus": 0.7, "transcript": 0.6, "kernel": 0.6, "exchange": 0.6},
}


def compute_mode_fit(tile, query_mode: str) -> float:
    """Score how well a tile's source_type fits the query mode.

    Returns score in [0, 1]. Unknown source_types get 0.5 (neutral).
    """
    source_type = getattr(tile, "source_type", None) or "transcript"
    fit_map = _MODE_FIT_MAP.get(query_mode, _MODE_FIT_MAP["default"])
    return fit_map.get(source_type, 0.5)


# ============================================================================
# Main Scorer
# ============================================================================

def apply_provenance_scoring(
    tiles: list,
    query_type: str = "default",
    query_mode: Optional[str] = None,
) -> list:
    """Apply provenance-weighted scoring to reranked tiles.

    This replaces the simple coherence_boost. Each tile's score is
    recomputed as a weighted combination of its existing score (hybrid +
    reranker) and provenance signals.

    Args:
        tiles: List of TileResult objects (already reranked)
        query_type: classifier output (exact, temporal, conceptual, etc.)
        query_mode: epistemic mode (canon, research, audit, exploration)
                   If None, inferred from query_type.

    Returns:
        Re-sorted list of tiles with updated scores.
    """
    if not tiles:
        return tiles

    # Infer query_mode from query_type if not specified
    if query_mode is None:
        query_mode = QUERY_TYPE_TO_MODE.get(query_type, "default")

    # Pre-compute source counts for independence scoring
    source_counts = Counter()
    for tile in tiles:
        src = getattr(tile, "source_cluster_id", None) or getattr(tile, "source_file", None) or "unknown"
        source_counts[src] += 1

    # Pre-compute echo penalties
    echo_penalties = compute_echo_penalties(tiles)

    # Pre-compute graph support (Neo4j neighbor votes) — skip if weight is 0
    graph_scores = compute_graph_support(tiles) if W_GRAPH > 0 else {}

    scored_tiles = []
    for tile in tiles:
        # All component signals are normalized to [0, 1] before weighting.
        # GTE cross-encoder outputs probabilities in [0, 1].
        raw_score = getattr(tile, "score", 0.0)
        norm_score = max(0.0, min(1.0, raw_score))

        # Component scores — each function returns [0, 1]
        pp = provenance_prior(tile, query_mode)       # [0, 1] clamped
        co = correction_obedience(tile)                # {0.0, 0.5, 0.9, 1.0}
        si = source_independence(tile, source_counts)  # [0, 1] via 1/sqrt(n)
        gs = graph_scores.get(getattr(tile, "content_hash", ""), 0.0)  # [0, 1] normalized
        tf = compute_temporal_freshness(tile, query_type)  # [0, 1] decay
        mf = compute_mode_fit(tile, query_mode)        # [0, 1] from map
        rp = retraction_penalty(tile, query_mode)      # {0.0, 1.0}
        cp = contradiction_penalty(tile)               # [0, 1] clamped

        key = getattr(tile, "content_hash", None) or id(tile)
        ep = echo_penalties.get(key, 0.0)

        # Combined score — mode-specific weight deltas (Phase 9)
        w = get_mode_weights(query_type)
        final = (
            w["hybrid"] * norm_score
            + w["provenance"] * pp
            + w["graph"] * gs
            + w["correction"] * co
            + w["independence"] * si
            + w["temporal"] * tf
            + w["mode_fit"] * mf
            - P_RETRACTION * rp
            - P_CONTRADICTION * cp
            - P_ECHO * ep
        )
        final = max(0.01, final)

        scored_tiles.append((final, tile, {
            "provenance_prior": round(pp, 4),
            "graph_support": round(gs, 4),
            "temporal_freshness": round(tf, 4),
            "mode_fit": round(mf, 4),
            "correction_obedience": round(co, 4),
            "source_independence": round(si, 4),
            "retraction_penalty": round(rp, 4),
            "contradiction_penalty": round(cp, 4),
            "echo_penalty": round(ep, 4),
            "normalized_relevance": round(norm_score, 4),
            "final_score": round(final, 4),
        }))

    # Sort by final score descending
    scored_tiles.sort(key=lambda x: x[0], reverse=True)

    # Update tile scores and attach provenance metadata
    result = []
    for final_score, tile, meta in scored_tiles:
        updated = dc_replace(tile, score=final_score)
        result.append(updated)

    # Log summary
    if result:
        top_meta = scored_tiles[0][2] if scored_tiles else {}
        log.info(
            "Provenance scoring: %d tiles, mode=%s, top_score=%.4f (pp=%.3f, gs=%.3f, co=%.3f)",
            len(result), query_mode,
            scored_tiles[0][0] if scored_tiles else 0,
            top_meta.get("provenance_prior", 0),
            top_meta.get("graph_support", 0),
            top_meta.get("correction_obedience", 0),
        )

    return result
