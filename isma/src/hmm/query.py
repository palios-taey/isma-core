"""
HMM Query - motif-based retrieval without vectors.

Original query loop (deprecated in favor of v2 adaptive_search):
  1. compile_query_to_motifs(text) -> motif vector
  2. candidate selection via motif inverted index + resonance weighting
  3. fetch tile metadata from Neo4j
  4. return tiles + provenance refs

Phase 5: retrieve() now delegates to ISMARetrievalV2.adaptive_search()
with fallback to the original Redis inverted index path.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple

from .motifs import assign_motifs, MotifAssignment
from .neo4j_store import HMMNeo4jStore
from .redis_store import HMMRedisStore

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Result of an HMM query."""
    query_motifs: List[MotifAssignment]
    candidates: List[Dict]  # scored tile results
    total_candidates: int
    resonance_boost_applied: bool = False


@dataclass
class TileResult:
    """A single tile result with score and provenance."""
    tile_id: str
    artifact_id: str
    index: int
    score: float
    motif_overlap: Dict[str, float]  # motif_id -> overlap score
    layer: str = ""
    scale: str = ""


class HMMQuery:
    """HMM query engine - retrieval without vectors."""

    def __init__(
        self,
        neo4j: Optional[HMMNeo4jStore] = None,
        redis: Optional[HMMRedisStore] = None,
    ):
        self.neo4j = neo4j or HMMNeo4jStore()
        self.redis = redis or HMMRedisStore()

    def close(self):
        if self.neo4j:
            self.neo4j.close()

    def compile_query_to_motifs(self, query_text: str) -> List[MotifAssignment]:
        """
        Convert query text to motif vector using same assignment as ingest.

        Tier 0: regex/heuristic triggers (same as motifs.assign_motifs).
        """
        return assign_motifs(query_text)

    def retrieve(
        self,
        query_text: str,
        top_k: int = 20,
        use_resonance: bool = True,
        require_all_motifs: bool = False,
        min_score: float = 0.0,
        use_v2: bool = True,
    ) -> QueryResult:
        """
        Full query loop: text -> motifs -> candidates -> scored results.

        Phase 5: Delegates to ISMARetrievalV2.adaptive_search() by default.
        Falls back to Redis inverted index if v2 is unavailable.

        Args:
            query_text: Natural language query
            top_k: Maximum results to return
            use_resonance: Boost scores with resonance fields
            require_all_motifs: If True, require ALL query motifs (intersect).
                                If False, require ANY query motif (union).
            min_score: Minimum score threshold
            use_v2: Use v2 adaptive search (default True). Set False for legacy.
        """
        # Phase 5: Try v2 adaptive search first
        if use_v2:
            try:
                from isma.src.retrieval_v2 import get_retrieval_v2
                r = get_retrieval_v2()
                if r.is_available():
                    v2_result = r.adaptive_search(query_text, top_k=top_k)
                    # Convert v2 result to QueryResult format
                    query_motifs = self.compile_query_to_motifs(query_text)
                    candidates = []
                    for tile in v2_result.get("tiles", []):
                        if hasattr(tile, "content_hash"):
                            candidates.append({
                                "tile_id": tile.content_hash,
                                "score": round(tile.score, 4),
                                "motif_overlap": {},
                                "rosetta_summary": tile.rosetta_summary,
                                "platform": tile.platform,
                            })
                    return QueryResult(
                        query_motifs=query_motifs,
                        candidates=candidates,
                        total_candidates=len(candidates),
                        resonance_boost_applied=False,
                    )
            except Exception as e:
                log.debug("V2 retrieve failed, falling back to Redis: %s", e)

        # Legacy path: Redis inverted index
        # Step 1: Compile query to motifs
        query_motifs = self.compile_query_to_motifs(query_text)

        if not query_motifs:
            return QueryResult(
                query_motifs=[],
                candidates=[],
                total_candidates=0,
            )

        motif_ids = [a.motif_id for a in query_motifs]
        motif_amps = {a.motif_id: a.amp for a in query_motifs}

        # Step 2: Candidate selection via inverted index
        if require_all_motifs:
            candidate_tiles = self.redis.inv_intersect(motif_ids)
        else:
            candidate_tiles = self.redis.inv_union(motif_ids)

        if not candidate_tiles:
            return QueryResult(
                query_motifs=query_motifs,
                candidates=[],
                total_candidates=0,
            )

        # Step 3: Score candidates
        resonance_fields = {}
        if use_resonance:
            for k in range(3):
                resonance_fields[k] = self.redis.field_get(k)

        scored = []
        for tile_id in candidate_tiles:
            score, overlap = self._score_tile(
                tile_id, motif_ids, motif_amps, resonance_fields
            )
            if score >= min_score:
                scored.append((tile_id, score, overlap))

        # Sort by score descending
        scored.sort(key=lambda x: x[1], reverse=True)
        total = len(scored)
        scored = scored[:top_k]

        # Step 4: Fetch tile metadata from Neo4j
        candidates = []
        for tile_id, score, overlap in scored:
            tile_meta = {}
            if self.neo4j:
                tile_motifs = self.neo4j.get_tile_motifs(tile_id)
                # Try to get basic tile info
                tile_info = self._get_tile_info(tile_id)
                tile_meta = tile_info

            candidates.append({
                "tile_id": tile_id,
                "score": round(score, 4),
                "motif_overlap": overlap,
                **tile_meta,
            })

        return QueryResult(
            query_motifs=query_motifs,
            candidates=candidates,
            total_candidates=total,
            resonance_boost_applied=use_resonance and bool(resonance_fields),
        )

    def retrieve_by_motifs(
        self,
        motif_ids: List[str],
        motif_weights: Optional[Dict[str, float]] = None,
        top_k: int = 20,
    ) -> QueryResult:
        """Retrieve directly by motif IDs (skip text compilation)."""
        if not motif_weights:
            motif_weights = {mid: 1.0 for mid in motif_ids}

        candidate_tiles = self.redis.inv_union(motif_ids)
        if not candidate_tiles:
            return QueryResult(
                query_motifs=[],
                candidates=[],
                total_candidates=0,
            )

        resonance_fields = {k: self.redis.field_get(k) for k in range(3)}

        scored = []
        for tile_id in candidate_tiles:
            score, overlap = self._score_tile(
                tile_id, motif_ids, motif_weights, resonance_fields
            )
            scored.append((tile_id, score, overlap))

        scored.sort(key=lambda x: x[1], reverse=True)
        total = len(scored)
        scored = scored[:top_k]

        candidates = []
        for tile_id, score, overlap in scored:
            tile_meta = self._get_tile_info(tile_id) if self.neo4j else {}
            candidates.append({
                "tile_id": tile_id,
                "score": round(score, 4),
                "motif_overlap": overlap,
                **tile_meta,
            })

        return QueryResult(
            query_motifs=[],
            candidates=candidates,
            total_candidates=total,
            resonance_boost_applied=True,
        )

    def _score_tile(
        self,
        tile_id: str,
        query_motifs: List[str],
        query_amps: Dict[str, float],
        resonance_fields: Dict[int, Dict[str, float]],
    ) -> Tuple[float, Dict[str, float]]:
        """
        Score a candidate tile against the query motifs.

        Score = weighted_overlap + resonance_boost
        """
        # Get tile's motif assignments from cache
        cached = self.redis.tile_cache_get(tile_id)
        if not cached:
            # Fallback: check which query motifs this tile is in
            overlap = {}
            for mid in query_motifs:
                if self.redis.r.sismember(f"hmm:inv:{mid}", tile_id):
                    overlap[mid] = query_amps.get(mid, 1.0)
        else:
            tile_motifs = {a["motif_id"]: a["amp"] for a in cached}
            overlap = {}
            for mid in query_motifs:
                if mid in tile_motifs:
                    overlap[mid] = min(
                        query_amps.get(mid, 1.0), tile_motifs[mid]
                    )

        if not overlap:
            return 0.0, {}

        # Weighted overlap score
        weighted_sum = sum(overlap.values())
        max_possible = sum(query_amps.get(mid, 1.0) for mid in query_motifs)
        base_score = weighted_sum / (max_possible or 1.0)

        # Resonance boost (optional)
        resonance_boost = 0.0
        if resonance_fields:
            for k, field_amps in resonance_fields.items():
                # Weight: fast field matters most for recency, slow for foundations
                k_weight = [0.5, 0.3, 0.2][k] if k < 3 else 0.1
                for mid in overlap:
                    field_amp = field_amps.get(mid, 0.0)
                    resonance_boost += k_weight * field_amp * 0.2

        final_score = min(1.0, base_score + resonance_boost)
        return final_score, overlap

    def _get_tile_info(self, tile_id: str) -> Dict:
        """Get basic tile info from Neo4j."""
        if not self.neo4j:
            return {}
        query = """
        MATCH (t:HMMTile {tile_id: $tile_id})
        OPTIONAL MATCH (a:HMMArtifact)-[:HAS_TILE]->(t)
        RETURN t.artifact_id AS artifact_id, t.index AS index,
               t.layer AS layer, t.scale AS scale,
               a.path AS artifact_path
        """
        with self.neo4j.driver.session() as session:
            result = session.run(query, tile_id=tile_id)
            record = result.single()
            if record:
                return dict(record)
        return {}
