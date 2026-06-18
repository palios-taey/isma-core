"""
ISMA Contradiction Detector — Cross-encoder verification of contradictions.

When HMM enrichment flags RELATES_TO {type: "contradicts"} edges, this module
verifies them using the Qwen3-Reranker-8B cross-encoder to score actual
semantic contradiction strength. Confirmed contradictions get promoted to
first-class CONTRADICTS edges with confidence scores.

Also detects contradictions between RELATES_TO neighbors by comparing
rosetta summaries of tiles that express the same motifs differently.

Usage:
    from isma.src.contradiction_detector import check_contradictions

    # After HMM enrichment writes a tile
    results = check_contradictions(tile_id="abc123def456")

    # Batch check all unverified contradictions
    results = check_contradictions_batch(limit=100)
"""

import logging
import time
from typing import Dict, List, Optional

from neo4j import GraphDatabase
from isma.config import NEO4J_URI

log = logging.getLogger(__name__)

# Minimum reranker score to confirm a contradiction
CONTRADICTION_THRESHOLD = 0.3

# Maximum neighbors to check per tile
MAX_NEIGHBORS = 20


def _get_driver():
    """Get Neo4j driver (shared singleton from neo4j_store)."""
    from isma.src.hmm.neo4j_store import get_shared_driver
    return get_shared_driver(NEO4J_URI)


def _get_reranker():
    """Get reranker client, return None if unavailable."""
    try:
        from isma.src.reranker import get_reranker
        reranker = get_reranker()
        if reranker.is_available():
            return reranker
    except Exception as e:
        log.debug("Reranker unavailable: %s", e)
    return None


def check_contradictions(tile_id: str) -> List[Dict]:
    """Check a tile's RELATES_TO {type: 'contradicts'} neighbors.

    For each flagged contradiction, uses the cross-encoder to verify:
    - Scores the pair (tile_a.rosetta, tile_b.rosetta) with instruction
      "Does statement A contradict statement B?"
    - If score > CONTRADICTION_THRESHOLD, creates CONTRADICTS edge

    Returns list of confirmed contradictions with confidence scores.
    """
    reranker = _get_reranker()
    if not reranker:
        log.debug("Reranker unavailable — skipping contradiction check for %s", tile_id)
        return []

    driver = _get_driver()
    confirmed = []

    try:
        with driver.session() as session:
            # Get existing contradicts-type RELATES_TO edges
            result = session.run("""
                MATCH (a:HMMTile {tile_id: $tile_id})-[r:RELATES_TO {type: 'contradicts'}]-(b:HMMTile)
                WHERE a.rosetta_summary IS NOT NULL AND b.rosetta_summary IS NOT NULL
                  AND NOT EXISTS { MATCH (a)-[:CONTRADICTS]-(b) }
                RETURN b.tile_id AS other_id,
                       a.rosetta_summary AS rosetta_a,
                       b.rosetta_summary AS rosetta_b,
                       r.note AS note
                LIMIT $limit
            """, tile_id=tile_id, limit=MAX_NEIGHBORS)

            pairs = [dict(r) for r in result]

        if not pairs:
            return []

        log.info("Checking %d contradiction candidates for tile %s", len(pairs), tile_id)

        # Score each pair with the reranker
        for pair in pairs:
            query = f"Does this statement contradict the reference? Statement: {pair['rosetta_a']}"
            # Create a minimal tile-like object for the reranker
            from isma.src.retrieval import TileResult
            doc_tile = TileResult(
                content=pair["rosetta_b"],
                content_hash=pair["other_id"],
                platform="", source_type="", source_file="",
                session_id="", document_id="", scale="",
                tile_id=pair["other_id"], token_count=0, score=0.0,
            )

            scored = reranker.rerank(
                query, [doc_tile],
                instruction="Identify if two statements are contradictory or incompatible",
                query_type="contradiction",
            )

            if scored and scored[0].score > CONTRADICTION_THRESHOLD:
                confidence = min(scored[0].score, 1.0)
                log.info("Confirmed contradiction: %s <-> %s (confidence=%.3f)",
                         tile_id, pair["other_id"], confidence)

                # Write CONTRADICTS edge
                with driver.session() as neo_session:
                    neo_session.run("""
                        MATCH (a:HMMTile {tile_id: $tile_a})
                        MATCH (b:HMMTile {tile_id: $tile_b})
                        MERGE (a)-[r:CONTRADICTS]->(b)
                        SET r.detected_at = $now,
                            r.confidence = $confidence,
                            r.resolution = '',
                            r.detected_by = 'reranker_crossencoder',
                            r.note = $note
                    """, tile_a=tile_id, tile_b=pair["other_id"],
                        now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        confidence=confidence,
                        note=pair.get("note", ""))

                confirmed.append({
                    "tile_a": tile_id,
                    "tile_b": pair["other_id"],
                    "confidence": confidence,
                    "note": pair.get("note", ""),
                })
            else:
                score = scored[0].score if scored else 0.0
                log.debug("Contradiction not confirmed: %s <-> %s (score=%.3f)",
                          tile_id, pair["other_id"], score)

    except Exception as e:
        log.error("Contradiction check failed for %s: %s", tile_id, e)

    return confirmed


def check_contradictions_batch(limit: int = 100) -> List[Dict]:
    """Batch check all RELATES_TO {type: 'contradicts'} edges that don't
    yet have a corresponding CONTRADICTS edge.

    Returns list of all confirmed contradictions.
    """
    driver = _get_driver()
    all_confirmed = []

    try:
        with driver.session() as session:
            # Find tiles with unverified contradicts edges
            result = session.run("""
                MATCH (a:HMMTile)-[r:RELATES_TO {type: 'contradicts'}]->(b:HMMTile)
                WHERE a.rosetta_summary IS NOT NULL
                  AND NOT EXISTS { MATCH (a)-[:CONTRADICTS]->(b) }
                RETURN DISTINCT a.tile_id AS tile_id
                LIMIT $limit
            """, limit=limit)

            tile_ids = [r["tile_id"] for r in result]

        log.info("Batch checking contradictions for %d tiles", len(tile_ids))

        for tid in tile_ids:
            confirmed = check_contradictions(tid)
            all_confirmed.extend(confirmed)

    except Exception as e:
        log.error("Batch contradiction check failed: %s", e)

    log.info("Batch complete: %d contradictions confirmed out of %d tiles checked",
             len(all_confirmed), len(tile_ids) if 'tile_ids' in dir() else 0)
    return all_confirmed
