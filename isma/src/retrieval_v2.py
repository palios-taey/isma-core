"""
ISMA Retrieval V2 — Document-level canonical memory search.

Searches ISMA_Quantum_v2 (one object per content_hash) using:
  - Named vector search: raw (content), rosetta (semantic summary)
  - BM25F text search with field weighting
  - RRF (Reciprocal Rank Fusion) of raw + rosetta + BM25 results
  - Neural reranking via Qwen3-Reranker-8B

Falls back to v1 (ISMARetrieval) if v2 class doesn't exist.

Usage:
    from isma.src.retrieval_v2 import ISMARetrievalV2

    r = ISMARetrievalV2()
    result = r.search("information retrieval example", top_k=10)
    result = r.hybrid_search("trust threshold", top_k=10)
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter

from isma.src.retrieval import (
    EMBEDDING_MODEL,
    EMBEDDING_URL,
    WEAVIATE_URL,
    TILE_PROPERTIES,
    SearchResult,
    TileResult,
    _get_embedding,
    _parse_tile,
)

log = logging.getLogger(__name__)

V2_CLASS = "ISMA_Quantum_v2"
V1_CLASS = "ISMA_Quantum"
COLBERT_CLASS = "ISMA_ColBERT"
MAX_THREAD_FANOUT = 4

# Quarantine: content hashes excluded from search results.
# 60c0df94b3a1271a = benchmark_20260309 results JSON (self-referential hub, was in top-3 of 25+ queries)
_QUARANTINE_HASHES = {
    "60c0df94b3a1271a",
    # "531cdee9fea575a6",  # tested: NET NEGATIVE for temporal when quarantine is active
}

# Lazy-loaded ColBERT model for MuVera multi-vector queries
_colbert_model = None
_colbert_lock = threading.Lock()
_colbert_available = None  # None = unknown, True/False = checked
_wv_local = threading.local()


def _get_colbert_model():
    """Load ColBERT model lazily (thread-safe, ~1s load on first call)."""
    global _colbert_model
    if _colbert_model is not None:
        return _colbert_model
    with _colbert_lock:
        if _colbert_model is not None:
            return _colbert_model
        try:
            from pylate import models
            _colbert_model = models.ColBERT("lightonai/answerai-colbert-small-v1")
            log.info("ColBERT model loaded for MuVera queries")
        except Exception as e:
            log.warning("ColBERT model unavailable: %s", e)
    return _colbert_model


def _colbert_collection_exists() -> bool:
    """Check if ISMA_ColBERT collection exists (cached)."""
    global _colbert_available
    if _colbert_available is not None:
        return _colbert_available
    try:
        r = requests.get(f"{WEAVIATE_URL}/v1/schema/{COLBERT_CLASS}", timeout=5)
        _colbert_available = r.status_code == 200
    except Exception:
        _colbert_available = False
    return _colbert_available


def _get_wv_session() -> requests.Session:
    session = getattr(_wv_local, "session", None)
    if session is None:
        session = requests.Session()
        session.mount("http://", HTTPAdapter(pool_connections=50, pool_maxsize=50))
        session.mount("https://", HTTPAdapter(pool_connections=50, pool_maxsize=50))
        _wv_local.session = session
    return session

# Properties to return from V1 tile searches (needed for TileResult + RRF fusion)
V1_TILE_PROPS = (
    "content content_hash platform source_type source_file session_id document_id "
    "loaded_at scale tile_index token_count hmm_enriched rosetta_summary "
    "dominant_motifs hmm_phi hmm_trust"
)

# Properties to fetch from v2 objects
V2_PROPERTIES = [
    "content", "content_hash", "platform", "source_type", "source_file",
    "session_id", "document_id", "loaded_at",
    "rosetta_summary", "motif_annotations", "dominant_motifs",
    "hmm_enriched", "hmm_phi", "hmm_trust", "hmm_enriched_at",
    "tile_count_512", "tile_count_2048", "tile_count_4096", "total_tokens",
    "tile_ids_512", "tile_ids_2048", "tile_ids_4096", "rosetta_tile_id",
]
V2_PROPS_STR = " ".join(V2_PROPERTIES)
STAGE_TILE_PROPS = " ".join(TILE_PROPERTIES)

# 6C.1 Query-aware alpha bands — BM25/dense weighting per query type
# alpha=0.0 → pure BM25, alpha=1.0 → pure vector
ALPHA_BANDS = {
    "exact": 0.3,       # Balanced — pure BM25 (0.0) fails on queries with common words
    "temporal": 0.3,    # Moderate vector — BM25 still important for date terms
    "conceptual": 0.65, # Same as default — alpha=0.8 regressed conceptual by -0.046
    "relational": 0.5,  # Balanced — graph does heavy lifting
    "memory": 0.55,     # Conversational recall benefits from hybrid with stronger lexical anchor
    "humor": 0.55,      # Humor queries need both semantic tone and exact phrase recovery
    "motif": 0.6,       # Moderate vector + motif boost
    "default": 0.65,    # Original V1 default
}


def _escape_gql(s: str) -> str:
    """Escape a string for embedding in a GraphQL value literal."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )


class RetrievalV2InfraError(RuntimeError):
    """Raised when V2 retrieval infrastructure fails explicitly."""


def _graphql(query: str) -> dict:
    """Execute a GraphQL query using connection-pooled session.

    Raises ConnectionError/Timeout on infrastructure failures so callers
    can surface them as 5xx rather than returning empty results.
    """
    try:
        r = _get_wv_session().post(
            f"{WEAVIATE_URL}/v1/graphql",
            json={"query": query},
            timeout=30,
        )
        result = r.json()
        # Weaviate returns HTTP 200 with {"data": null, "errors": [...]} on invalid GQL syntax.
        # data.get("data", {}) returns None (key exists, value is null) → AttributeError on .get().
        if result.get("data") is None:
            if result.get("errors"):
                raise RetrievalV2InfraError(f"GraphQL errors: {result['errors']}")
            raise RetrievalV2InfraError("GraphQL returned null data")
        return result
    except (requests.ConnectionError, requests.Timeout) as e:
        log.error("Weaviate connection failure: %s", e)
        raise
    except Exception as e:
        if isinstance(e, RetrievalV2InfraError):
            raise
        log.warning("GraphQL error: %s", e)
        raise RetrievalV2InfraError(str(e)) from e


def _v2_to_tile(obj: dict, score: float = 0.0) -> TileResult:
    """Convert a v2 object to a TileResult for API compatibility."""
    return TileResult(
        content=obj.get("content", ""),
        content_hash=obj.get("content_hash", ""),
        platform=obj.get("platform", ""),
        source_type=obj.get("source_type", ""),
        source_file=obj.get("source_file", ""),
        session_id=obj.get("session_id", ""),
        document_id=obj.get("document_id", ""),
        loaded_at=obj.get("loaded_at", ""),
        scale="canonical",  # v2 objects are document-level
        tile_id="",
        token_count=obj.get("total_tokens") or 0,
        score=score,
        hmm_enriched=obj.get("hmm_enriched", False),
        rosetta_summary=obj.get("rosetta_summary") or "",
        dominant_motifs=obj.get("dominant_motifs") or [],
        hmm_phi=obj.get("hmm_phi") or 0.0,
        hmm_trust=obj.get("hmm_trust") or 0.0,
    )


class ISMARetrievalV2:
    """V2 retrieval using canonical memory objects."""

    def __init__(self):
        self._v2_available: Optional[bool] = None

    def is_available(self) -> bool:
        """Check if the v2 class exists in Weaviate."""
        if self._v2_available is not None:
            return self._v2_available
        try:
            r = requests.get(f"{WEAVIATE_URL}/v1/schema/{V2_CLASS}", timeout=5)
            self._v2_available = r.status_code == 200
        except Exception:
            self._v2_available = False
        return self._v2_available

    def stats(self) -> dict:
        """Get v2 collection statistics."""
        data = _graphql(
            f"{{ Aggregate {{ {V2_CLASS} {{ meta {{ count }} }} }} }}"
        )
        total = (
            data.get("data", {})
            .get("Aggregate", {})
            .get(V2_CLASS, [{}])[0]
            .get("meta", {})
            .get("count", 0)
        )

        # Count enriched
        data_e = _graphql(
            f'{{ Aggregate {{ {V2_CLASS}(where: {{ path: ["hmm_enriched"] '
            f'operator: Equal valueBoolean: true }}) {{ meta {{ count }} }} }} }}'
        )
        enriched = (
            data_e.get("data", {})
            .get("Aggregate", {})
            .get(V2_CLASS, [{}])[0]
            .get("meta", {})
            .get("count", 0)
        )

        return {
            "v2_total": total,
            "v2_enriched": enriched,
            "v2_available": self.is_available(),
        }

    # ── Vector Search ───────────────────────────────────────────

    def search_raw(
        self,
        query: str,
        top_k: int = 10,
        **filters,
    ) -> List[Tuple[dict, float]]:
        """Search using the raw (content_512) named vector."""
        embedding = _get_embedding(query)
        if not embedding:
            return []

        filter_clause = self._build_filter(**filters)
        vector_str = str(embedding)

        q = (
            f"{{ Get {{ {V2_CLASS}("
            f"nearVector: {{ vector: {vector_str}, targetVectors: [\"raw\"] }}"
            f" limit: {top_k}"
            f"{filter_clause}"
            f") {{ {V2_PROPS_STR} _additional {{ score distance }} }} }} }}"
        )

        data = _graphql(q)
        results = data.get("data", {}).get("Get", {}).get(V2_CLASS, [])

        return [
            (obj, float(obj.get("_additional", {}).get("score") or 0))
            for obj in results
        ]

    def search_rosetta(
        self,
        query: str,
        top_k: int = 10,
        vector: Optional[list] = None,
        **filters,
    ) -> List[Tuple[dict, float]]:
        """Search using the rosetta (summary) named vector."""
        embedding = vector or _get_embedding(query)
        if not embedding:
            return []

        filter_clause = self._build_filter(**filters)
        vector_str = str(embedding)

        q = (
            f"{{ Get {{ {V2_CLASS}("
            f"nearVector: {{ vector: {vector_str}, targetVectors: [\"rosetta\"] }}"
            f" limit: {top_k}"
            f"{filter_clause}"
            f") {{ {V2_PROPS_STR} _additional {{ score distance }} }} }} }}"
        )

        data = _graphql(q)
        results = data.get("data", {}).get("Get", {}).get(V2_CLASS, [])

        return [
            (obj, float(obj.get("_additional", {}).get("score") or 0))
            for obj in results
        ]

    # ── BM25 Search ─────────────────────────────────────────────

    def search_bm25(
        self,
        query: str,
        top_k: int = 10,
        **filters,
    ) -> List[Tuple[dict, float]]:
        """BM25F text search with field weighting.

        Weaviate BM25 properties weight is applied via the query.
        rosetta_summary and motif_annotations get boosted.
        """
        # Escape query for GraphQL (backslash first, then quote, then newlines)
        safe_query = query.replace('\\', '\\\\').replace('"', '\\"').replace('\n', ' ').replace('\r', ' ')
        filter_clause = self._build_filter(**filters)

        q = (
            f"{{ Get {{ {V2_CLASS}("
            f'bm25: {{ query: "{safe_query}" '
            f'properties: ["rosetta_summary^3", "motif_annotations^2", "content"] }}'
            f" limit: {top_k}"
            f"{filter_clause}"
            f") {{ {V2_PROPS_STR} _additional {{ score }} }} }} }}"
        )

        data = _graphql(q)
        results = data.get("data", {}).get("Get", {}).get(V2_CLASS, [])

        return [
            (obj, float(obj.get("_additional", {}).get("score") or 0))
            for obj in results
        ]

    # ── V1 Tile Search (Option E paths) ─────────────────────────

    def search_v1_bm25(
        self,
        query: str,
        top_k: int = 30,
    ) -> List[Tuple[dict, float]]:
        """BM25 search on ISMA_Quantum search_512 tiles (full tile text, no truncation).

        This is the Option E replacement for the broken V2 BM25, which was limited
        to the first tile's text (2048 chars). V1 tiles contain the real passage text.
        """
        safe_query = (
            query.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", " ")
            .replace("\r", " ")
        )
        scale_filter = '{ path: ["scale"], operator: Equal, valueText: "search_512" }'
        q = (
            f'{{ Get {{ {V1_CLASS}('
            f'bm25: {{ query: "{safe_query}" properties: ["content^2", "rosetta_summary"] }}'
            f', where: {scale_filter}'
            f' limit: {top_k}'
            f') {{ {V1_TILE_PROPS} _additional {{ score }} }} }} }}'
        )
        data = _graphql(q)
        results = data.get("data", {}).get("Get", {}).get(V1_CLASS, []) or []
        return [
            (obj, float(obj.get("_additional", {}).get("score") or 0))
            for obj in results
        ]

    def search_v1_vector(
        self,
        query: str,
        top_k: int = 30,
        vector: Optional[list] = None,
    ) -> List[Tuple[dict, float]]:
        """NearVector search on ISMA_Quantum search_512 tiles.

        This is the Option E replacement for the broken V2 raw vector, which was
        embedded from the first tile only (2048 chars). V1 tile vectors represent
        the actual passage content.
        """
        embedding = vector or _get_embedding(query)
        if not embedding:
            return []
        vector_str = str(embedding)
        scale_filter = '{ path: ["scale"], operator: Equal, valueText: "search_512" }'
        q = (
            f'{{ Get {{ {V1_CLASS}('
            f'nearVector: {{ vector: {vector_str} }}'
            f', where: {scale_filter}'
            f' limit: {top_k}'
            f') {{ {V1_TILE_PROPS} _additional {{ score distance }} }} }} }}'
        )
        data = _graphql(q)
        results = data.get("data", {}).get("Get", {}).get(V1_CLASS, []) or []
        return [
            # nearVector returns distance (lower=better), convert to score (1-distance)
            (obj, 1.0 - float(obj.get("_additional", {}).get("distance") or 1.0))
            for obj in results
        ]

    def _v1_tile_to_obj(self, tile: dict) -> dict:
        """Convert a V1 ISMA_Quantum tile result to V2-compatible obj dict for RRF fusion."""
        return {
            "content": tile.get("content", ""),
            "content_hash": tile.get("content_hash", ""),
            "platform": tile.get("platform", ""),
            "source_type": tile.get("source_type", ""),
            "source_file": tile.get("source_file", ""),
            "session_id": tile.get("session_id", ""),
            "document_id": tile.get("document_id", ""),
            "loaded_at": tile.get("loaded_at", ""),
            "rosetta_summary": tile.get("rosetta_summary", "") or "",
            "dominant_motifs": tile.get("dominant_motifs") or [],
            "hmm_enriched": tile.get("hmm_enriched", False),
            "hmm_phi": tile.get("hmm_phi") or 0.0,
            "hmm_trust": tile.get("hmm_trust") or 0.0,
            "total_tokens": tile.get("token_count") or 0,
            "tile_ids_512": [], "tile_ids_2048": [], "tile_ids_4096": [],
            "tile_count_512": 0, "tile_count_2048": 0, "tile_count_4096": 0,
            "motif_annotations": "",
        }

    # ── Hybrid Search (RRF Fusion) ──────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        **filters,
    ) -> SearchResult:
        """Option E hybrid search: V2 rosetta + V1 BM25 + V1 nearVector via parallel RRF.

        V2 raw nearVector and V2 BM25 are disabled — they were trained on the first
        search_512 tile only (2048 chars), causing -28pt exact recall regression.

        Paths (run in parallel):
          - V2 rosetta nearVector: semantic summary signal (conceptual strength)
          - V1 search_512 BM25: full passage text coverage (exact/temporal strength)
          - V1 search_512 nearVector: per-tile embeddings (local evidence recovery)

        Results fused at content_hash level via RRF (k=60).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t0 = time.monotonic()
        fetch_k = max(top_k * 3, 30)

        # Embed query ONCE — shared by rosetta nearVector + V1 nearVector paths.
        # BM25 path needs no embedding. Embedding twice in parallel wastes server capacity
        # and doubles latency under load (ColBERT ingest, etc.).
        query_vector = _get_embedding(query)

        # Run all three paths in parallel
        futures_map = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures_map["rosetta"] = executor.submit(
                self.search_rosetta, query, top_k=fetch_k, vector=query_vector, **filters
            )
            futures_map["v1_bm25"] = executor.submit(
                self.search_v1_bm25, query, top_k=fetch_k
            )
            futures_map["v1_vector"] = executor.submit(
                self.search_v1_vector, query, top_k=fetch_k, vector=query_vector
            )
            results_by_path = {}
            for name, fut in futures_map.items():
                try:
                    results_by_path[name] = fut.result(timeout=25)
                except Exception as e:
                    log.warning("Search path %s failed: %s", name, e)
                    results_by_path[name] = []

        # RRF fusion at content_hash level (k=60 standard)
        # Two separate maps:
        #   v2_meta_map: V2 doc objects for metadata (rosetta_summary, dominant_motifs, etc.)
        #   v1_content_map: best V1 passage tile for each hash (for content — the specific passage)
        # V1 tile content is passage-level (~512 tokens), fitting within the 2000-char recall window.
        # V2 doc content is full concatenated document — evidence buried deep, fails recall[:2000].
        k = 60
        rrf_scores: Dict[str, float] = {}
        v2_meta_map: Dict[str, dict] = {}
        v1_content_map: Dict[str, dict] = {}  # hash → best V1 tile (first hit = highest scored)

        for rank, (obj, _score) in enumerate(results_by_path.get("rosetta", [])):
            ch = obj.get("content_hash", "")
            if ch:
                rrf_scores[ch] = rrf_scores.get(ch, 0) + 1.0 / (k + rank + 1)
                v2_meta_map[ch] = obj  # V2 obj: rosetta_summary, dominant_motifs, etc.

        for path in ("v1_bm25", "v1_vector"):
            for rank, (tile, _score) in enumerate(results_by_path.get(path, [])):
                ch = tile.get("content_hash", "")
                if ch:
                    rrf_scores[ch] = rrf_scores.get(ch, 0) + 1.0 / (k + rank + 1)
                    if ch not in v1_content_map:
                        v1_content_map[ch] = tile  # First (highest scored) V1 tile for this doc

        # Sort by RRF score
        sorted_hashes = sorted(rrf_scores.keys(), key=lambda ch: rrf_scores[ch], reverse=True)

        tiles = []
        for ch in sorted_hashes[:top_k]:
            v2_meta = v2_meta_map.get(ch)
            v1_tile = v1_content_map.get(ch)

            if v1_tile:
                # Build from V1 tile content + V2 metadata overlay
                obj = self._v1_tile_to_obj(v1_tile)
                if v2_meta:
                    # Overlay V2 metadata (richer: rosetta_summary, motifs, hmm_*)
                    obj["rosetta_summary"] = v2_meta.get("rosetta_summary", "") or obj["rosetta_summary"]
                    obj["dominant_motifs"] = v2_meta.get("dominant_motifs") or obj["dominant_motifs"]
                    obj["hmm_phi"] = v2_meta.get("hmm_phi") or obj["hmm_phi"]
                    obj["hmm_trust"] = v2_meta.get("hmm_trust") or obj["hmm_trust"]
                    obj["hmm_enriched"] = v2_meta.get("hmm_enriched", False) or obj["hmm_enriched"]
                    obj["motif_annotations"] = v2_meta.get("motif_annotations", "") or obj["motif_annotations"]
            elif v2_meta:
                # Only found via rosetta — use V2 obj (content will be concatenated but rosetta helps)
                obj = v2_meta
            else:
                continue

            tile_result = _v2_to_tile(obj, score=rrf_scores[ch])
            tiles.append(tile_result)

        elapsed_ms = (time.monotonic() - t0) * 1000
        total_tokens = sum(t.token_count for t in tiles)

        return SearchResult(
            query=query,
            tiles=tiles,
            total_tokens=total_tokens,
            search_time_ms=elapsed_ms,
        )

    # ── Hybrid Search with Reranker ─────────────────────────────

    def hybrid_search(
        self,
        query: str,
        top_k: int = 10,
        rerank: bool = True,
        query_type: str = "default",
        instruction: str = "",
        **filters,
    ) -> Dict[str, Any]:
        """Full hybrid search with neural reranking.

        1. RRF fusion of raw + rosetta + BM25
        2. Neural reranker on fused candidates
        3. Return top_k results
        """
        t0 = time.monotonic()

        # Get more candidates for reranking
        fetch_k = top_k * 3 if rerank else top_k
        search_result = self.search(query, top_k=fetch_k, **filters)

        # Neural reranking
        if rerank and search_result.tiles:
            try:
                from isma.src.reranker import get_reranker
                reranker = get_reranker()
                if reranker.is_available():
                    reranked = reranker.rerank(
                        query, search_result.tiles,
                        instruction=instruction,
                        query_type=query_type,
                    )
                    search_result = SearchResult(
                        query=query,
                        tiles=reranked[:top_k],
                        total_tokens=sum(t.token_count for t in reranked[:top_k]),
                        search_time_ms=search_result.search_time_ms,
                    )
            except Exception as e:
                log.debug("Reranker unavailable: %s", e)

        elapsed_ms = (time.monotonic() - t0) * 1000
        return {
            "query": query,
            "tiles": search_result.tiles[:top_k],
            "total_tokens": search_result.total_tokens,
            "search_time_ms": elapsed_ms,
            "hmm_reranked": rerank,
            "version": "v2",
        }

    # ── Adaptive Search ─────────────────────────────────────────

    def adaptive_search(
        self,
        query: str,
        top_k: int = 10,
        expand_graph: bool = True,
        graph_depth: int = 2,
        **filters,
    ) -> Dict[str, Any]:
        """Query-adaptive search — V1-Plus architecture.

        Classifies the query, routes to the appropriate V1-Plus strategy,
        applies temporal decay post-hoc, and returns reranked results.

        All non-relational strategies use V1 hybrid_retrieve_hmm as the base,
        with lazy V2 metadata overlay before the Qwen3-Reranker-8B step.

        Strategies:
          - exact:      V1 hybrid + V2 overlay + reranker (factual precision)
          - temporal:   V1 hybrid + V2 overlay + reranker + post-hoc decay
          - conceptual: V1 hybrid + V2 overlay + reranker + theme-motif logging
          - relational: parallel sub-queries + RRF + graph expansion (unchanged)
          - motif:      V1 motif-filtered hybrid + Redis/Neo4j motif RRF + V2 overlay
          - default:    V1 hybrid + V2 overlay + reranker
        """
        from isma.src.query_classifier import classify_query
        from isma.src.temporal_query import apply_temporal_decay, HALF_LIVES
        from isma.src.retrieval import get_retrieval

        t0 = time.monotonic()

        plan = classify_query(query)
        strategy = plan.strategy

        # Merge classifier-detected filters with explicit filters
        merged_filters = dict(filters)
        if plan.detected_platform and "platform" not in merged_filters:
            merged_filters["platform"] = plan.detected_platform

        # 6C.2 Temporal prefilter — uses `timestamp` field (source conversation time)
        # instead of `loaded_at` (ingest time). ISO 8601 lexicographic comparison works.
        if strategy == "temporal" and plan.temporal_window:
            tw = plan.temporal_window
            if tw.get("after") and "time_after" not in merged_filters:
                merged_filters["time_after"] = tw["after"]
            if tw.get("before") and "time_before" not in merged_filters:
                merged_filters["time_before"] = tw["before"]
            # "recent"/"latest" → last 30 days from now
            if tw.get("recent") and "time_after" not in merged_filters:
                from datetime import datetime, timedelta
                recent = str(tw["recent"]).strip()
                if recent.endswith("d"):
                    recent = recent[:-1]
                if not recent.isdigit():
                    raise ValueError(f"invalid temporal window: {tw['recent']!r}")
                days = int(recent)
                merged_filters["time_after"] = (
                    datetime.utcnow() - timedelta(days=days)
                ).strftime("%Y-%m-%d")

        v1 = get_retrieval()

        # Route to strategy
        if strategy == "relational" and plan.sub_queries and len(plan.sub_queries) >= 2 and self._relational_concepts_usable(plan.sub_queries):
            # Dream Cycle architecture (5/5 unanimous, March 2026):
            # Bi-Substrate Cascading Membrane — motif intersection + vector decomposition.
            # Requires 2+ usable sub-queries (concept_A + concept_B) for cascade.
            # Single-concept or weak-concept queries fall through to V1-Plus.
            result = self._search_relational_cascade(
                query, plan.sub_queries, top_k,
                instruction=plan.reranker_instruction,
                v1=v1,
                **merged_filters,
            )
        elif strategy == "motif" and plan.detected_motifs:
            # Wire detected_motifs into retrieval (previously ignored → R@10=0.000)
            result = self._search_motif(
                query, plan.detected_motifs, top_k,
                instruction=plan.reranker_instruction,
                v1=v1,
                **merged_filters,
            )
        elif strategy in {"memory", "humor", "temporal"}:
            # Complex path — queries that benefit from graph/motif expansion
            query_motifs = list(dict.fromkeys(
                (plan.detected_motifs or []) + self._get_theme_motifs(query)
            ))
            result = self.composed_search(
                query, top_k,
                instruction=plan.reranker_instruction,
                query_type=strategy,
                v1=v1,
                expand_graph=expand_graph,
                graph_depth=graph_depth,
                query_motifs=query_motifs,
                semantic_query=plan.semantic_query,
                **merged_filters,
            )
        else:
            # Simple path — factual, conceptual, infra, exact, default
            # Basic vector+BM25 already works well. Don't add entropy.
            result_sr = v1.hybrid_retrieve_hmm(
                query, top_k=top_k,
                hmm_rerank_enabled=False,
                expand_graph=False,
                **merged_filters,
            )
            # Apply epistemic tier boost for kernel docs
            tiles = result_sr.get("tiles", [])
            if tiles:
                from dataclasses import replace as dc_replace
                boosted = []
                for tile in tiles:
                    src = (tile.source_file or "").lower()
                    src_type = (tile.source_type or "").lower()
                    boost = 0.0
                    if src_type in ("layer0", "kernel") or "family_kernel" in src or "identity_logos" in src:
                        boost = 0.30
                    elif src_type == "document":
                        boost = 0.05
                    elif any(k in src for k in ("heartbeat", "health_check", "status_ping")):
                        boost = -0.25
                    boosted.append(dc_replace(tile, score=float(tile.score or 0) + boost))
                boosted.sort(key=lambda t: float(t.score or 0), reverse=True)
                result_sr["tiles"] = boosted[:top_k]
            result = result_sr

        # Apply temporal decay post-hoc for temporal queries
        if strategy == "temporal":
            half_life = HALF_LIVES.get(strategy, 90)
            tiles = result.get("tiles", [])
            if tiles:
                result["tiles"] = apply_temporal_decay(
                    tiles, half_life_days=half_life, decay_weight=0.15,
                )

        elapsed_ms = (time.monotonic() - t0) * 1000
        result["search_time_ms"] = elapsed_ms
        result["strategy"] = strategy
        result["query_plan"] = {
            "strategy": plan.strategy,
            "confidence": plan.confidence,
            "detected_platform": plan.detected_platform,
            "detected_motifs": plan.detected_motifs,
            "temporal_window": plan.temporal_window,
        }

        # 6C.0 Instrumentation: ensure diagnostics present
        if "diagnostics" not in result:
            result["diagnostics"] = {}
        result["diagnostics"]["strategy"] = strategy
        result["diagnostics"]["classifier_confidence"] = plan.confidence

        # Quarantine filter: remove benchmark pollution from results
        if _QUARANTINE_HASHES and result.get("tiles"):
            pre_count = len(result["tiles"])
            result["tiles"] = [
                t for t in result["tiles"]
                if t.content_hash not in _QUARANTINE_HASHES
            ]
            quarantined = pre_count - len(result["tiles"])
            if quarantined:
                log.info("Quarantined %d tiles (benchmark pollution)", quarantined)

        return result

    def composed_search(
        self,
        query: str,
        top_k: int,
        instruction: str,
        query_type: str,
        v1,
        expand_graph: bool = True,
        graph_depth: int = 1,
        query_motifs: Optional[List[str]] = None,
        semantic_query: Optional[str] = None,
        **filters,
    ) -> Dict[str, Any]:
        """Five-stage retrieval pipeline for non-relational adaptive search."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from dataclasses import replace as dc_replace

        t0 = time.monotonic()
        timings: Dict[str, float] = {}
        query_motifs = list(dict.fromkeys(query_motifs or []))

        alpha = ALPHA_BANDS.get(query_type, ALPHA_BANDS["default"])
        fetch_k = max(top_k * 3, top_k)

        # === PARALLEL: Vector+BM25 AND Motif Anchor concurrently ===
        stage1_t0 = time.monotonic()

        def _vector_path():
            return v1.search(
                query, top_k=fetch_k, scale="search_512",
                expand_parents=False, alpha=alpha,
                vector_query=semantic_query, **filters,
            )

        def _motif_path():
            if not query_motifs:
                return []
            results = []
            for mid in query_motifs[:3]:
                try:
                    mr = v1.motif_search(mid, min_amplitude=0.3, limit=fetch_k)
                    if mr and mr.tiles_with_amplitude:
                        # Fetch actual tile content from Weaviate for each motif hit
                        for item in mr.tiles_with_amplitude[:fetch_k]:
                            th = item.get("tile_hash", item.get("content_hash", ""))
                            if not th:
                                continue
                            tile_results = v1.get_tiles_for_content(th)
                            if tile_results:
                                results.append(tile_results[0])
                except Exception:
                    pass
            return results

        with ThreadPoolExecutor(max_workers=2) as pool:
            vec_future = pool.submit(_vector_path)
            mot_future = pool.submit(_motif_path)
            try:
                search_result = vec_future.result()
                vector_tiles = search_result.tiles or []
            except Exception:
                search_result = SearchResult(query=query, tiles=[])
                vector_tiles = []
            try:
                motif_anchor_tiles = mot_future.result() or []
            except Exception:
                motif_anchor_tiles = []

        # RRF fusion when motif path returned results
        if motif_anchor_tiles:
            rrf_k = 60
            rrf_scores: Dict[str, float] = {}
            tile_lookup: Dict[str, TileResult] = {}
            for rank, tile in enumerate(vector_tiles):
                ch = tile.content_hash
                if ch:
                    rrf_scores[ch] = rrf_scores.get(ch, 0.0) + 1.0 / (rrf_k + rank + 1)
                    if ch not in tile_lookup:
                        tile_lookup[ch] = tile
            for rank, tile in enumerate(motif_anchor_tiles):
                ch = getattr(tile, 'content_hash', None)
                if ch:
                    rrf_scores[ch] = rrf_scores.get(ch, 0.0) + 1.0 / (rrf_k + rank + 1)
                    if ch not in tile_lookup:
                        tile_lookup[ch] = tile
            fused = sorted(rrf_scores.keys(), key=lambda h: -rrf_scores[h])[:fetch_k]
            fused_tiles = [tile_lookup[h] for h in fused if h in tile_lookup]
            search_result = SearchResult(query=query, tiles=fused_tiles)

        timings["stage1_ms"] = round((time.monotonic() - stage1_t0) * 1000, 1)
        timings["motif_anchor_count"] = len(motif_anchor_tiles) if 'motif_anchor_tiles' in dir() else 0
        if not search_result.tiles:
            return {
                "query": query,
                "tiles": [],
                "total_tokens": 0,
                "hmm_reranked": False,
                "version": "composed_v1",
                "diagnostics": {"candidate_count": 0, "stage_timings_ms": timings},
            }

        candidate_state: Dict[str, Dict[str, Any]] = {}

        def _ensure_state(tile: TileResult, origin: str, path: Optional[dict] = None):
            ch = tile.content_hash
            if not ch:
                return
            state = candidate_state.get(ch)
            if state is None:
                state = {
                    "content_hash": ch,
                    "search_tile": tile if tile.scale == "search_512" else None,
                    "context_tile": None,
                    "full_tile": None,
                    "base_score": float(tile.score or 0.0),
                    "graph_bonus": 0.0,
                    "graph_paths": [],
                    "origins": {origin},
                }
                candidate_state[ch] = state
            else:
                if tile.scale == "search_512" and state["search_tile"] is None:
                    state["search_tile"] = tile
                state["base_score"] = max(state["base_score"], float(tile.score or 0.0))
                state["origins"].add(origin)
            if path:
                state["graph_paths"].append(path)
                state["graph_bonus"] = 1.0

        for rank, tile in enumerate(search_result.tiles):
            _ensure_state(
                tile,
                origin="direct",
                path={"source": "direct", "seed_hash": tile.content_hash, "rank": rank + 1},
            )

        stage2_t0 = time.monotonic()
        graph_neighbors = 0
        if expand_graph:
            seeds = [t for t in search_result.tiles[:5] if t.content_hash]
            if seeds:
                fetched_neighbors: Dict[str, dict] = {}

                def _expand(seed_tile: TileResult):
                    return seed_tile, v1.graph_expand(seed_tile.content_hash, depth=max(1, graph_depth))

                with ThreadPoolExecutor(max_workers=min(len(seeds), MAX_THREAD_FANOUT)) as pool:
                    futures = [pool.submit(_expand, seed) for seed in seeds]
                    for fut in as_completed(futures):
                        try:
                            seed_tile, neighbors = fut.result()
                        except Exception as e:
                            log.debug("Graph expansion lane failed: %s", e)
                            continue
                        for nb in (neighbors.related_tiles if neighbors else []):
                            ch = nb.get("tile_hash") or nb.get("content_hash")
                            if not ch:
                                continue
                            path = {
                                "source": "graph",
                                "seed_hash": seed_tile.content_hash,
                                "edge_type": nb.get("rel_type", ""),
                                "edge_note": nb.get("rel_note", ""),
                                "distance": nb.get("distance", 1),
                            }
                            state = candidate_state.get(ch)
                            if state is not None:
                                state["graph_paths"].append(path)
                                state["graph_bonus"] = 1.0
                                state["origins"].add("graph")
                                continue
                            if ch not in fetched_neighbors:
                                fetched_neighbors[ch] = path

                if fetched_neighbors:
                    for tile in self._fetch_v1_tiles_by_hash(list(fetched_neighbors.keys()), v1):
                        _ensure_state(tile, origin="graph", path=fetched_neighbors.get(tile.content_hash))
                        graph_neighbors += 1
        timings["stage2_ms"] = round((time.monotonic() - stage2_t0) * 1000, 1)

        ranked_pool = sorted(
            candidate_state.values(),
            key=lambda s: (s["base_score"] + 0.05 * s["graph_bonus"]),
            reverse=True,
        )
        stage3_candidates = [s["search_tile"] for s in ranked_pool if s["search_tile"]][:10]

        stage3_t0 = time.monotonic()
        context_tiles: Dict[str, TileResult] = {}
        full_tiles: Dict[str, TileResult] = {}
        if stage3_candidates:
            with ThreadPoolExecutor(max_workers=2) as pool:
                context_future = pool.submit(self._fetch_overlap_tiles, stage3_candidates, "context_2048")
                full_future = pool.submit(self._fetch_overlap_tiles, stage3_candidates[:3], "full_4096")
                try:
                    context_tiles = context_future.result()
                except Exception as e:
                    log.debug("Context escalation failed: %s", e)
                try:
                    full_tiles = full_future.result()
                except Exception as e:
                    log.debug("Full escalation failed: %s", e)
        for ch, tile in context_tiles.items():
            if ch in candidate_state:
                candidate_state[ch]["context_tile"] = tile
        for ch, tile in full_tiles.items():
            if ch in candidate_state:
                candidate_state[ch]["full_tile"] = tile
        timings["stage3_ms"] = round((time.monotonic() - stage3_t0) * 1000, 1)

        stage4_t0 = time.monotonic()
        rerank_pool: List[TileResult] = []
        rerank_lookup: Dict[str, Dict[str, Any]] = {}
        for state in ranked_pool[:10]:
            tile = state["search_tile"]
            if tile is None:
                continue
            rerank_pool.append(tile)
            rerank_lookup[tile.content_hash] = state

        reranked_tiles = list(rerank_pool)
        reranker_used = False
        if rerank_pool:
            try:
                from isma.src.reranker import get_reranker
                reranker = get_reranker()
                if reranker.is_available():
                    reranked_tiles = reranker.rerank(
                        query,
                        rerank_pool,
                        instruction=instruction,
                        query_type=query_type,
                    )
                    reranker_used = True
            except Exception as e:
                log.debug("Composed reranker unavailable: %s", e)

        if reranker_used:
            base_scores = {
                t.content_hash: float(t.score or 0.0)
                for t in reranked_tiles
                if t.content_hash
            }
        else:
            base_scores = self._normalize_tile_scores(reranked_tiles)
        assembled_tiles: List[TileResult] = []
        motif_details: Dict[str, Dict[str, Any]] = {}

        # Gemini audit: epistemic tier + conditional HMM gate
        _factual_types = {"exact", "infra"}
        _relational_types = {"relational", "memory", "humor", "bristle_arc", "conceptual", "motif"}
        _use_motif_boost = query_type in _relational_types

        for rank, tile in enumerate(reranked_tiles):
            state = rerank_lookup.get(tile.content_hash)
            if state is None:
                continue
            motif_overlap, matched_motifs = self._motif_overlap(query_motifs, tile.dominant_motifs or [])
            recency = self._recency_score(tile.loaded_at or (state["search_tile"].loaded_at if state["search_tile"] else ""))

            # Epistemic tier: penalize operational noise, boost foundational docs
            epistemic_penalty = 0.0
            src = (tile.source_file or "").lower()
            src_type = (tile.source_type or "").lower()
            if "heartbeat" in src or "health_check" in src or "status_ping" in src:
                epistemic_penalty = -0.15  # Operational noise penalized
            if src_type in ("layer0", "kernel"):
                epistemic_penalty = 0.10  # Foundational docs strongly boosted
            elif src_type == "document":
                epistemic_penalty = 0.05  # Documents mildly boosted

            # Conditional HMM gate: motifs help relational, hurt factual
            motif_weight = 0.2 if _use_motif_boost else 0.0
            base_weight = 0.6 if _use_motif_boost else 0.75

            composite = (
                base_scores.get(tile.content_hash, 0.0) * base_weight
                + motif_overlap * motif_weight
                + state["graph_bonus"] * 0.1
                + recency * 0.05
                + epistemic_penalty
            )
            primary = dc_replace(tile, score=composite)
            setattr(primary, "graph_path", state["graph_paths"] or [{"source": "direct", "seed_hash": primary.content_hash, "rank": rank + 1}])
            setattr(
                primary,
                "motif_annotations",
                {
                    "query_motifs": query_motifs,
                    "matched_motifs": matched_motifs,
                    "overlap": round(motif_overlap, 4),
                },
            )
            setattr(
                primary,
                "multi_scale",
                self._build_multi_scale_payload(
                    state["search_tile"],
                    state["context_tile"],
                    state["full_tile"],
                ),
            )
            assembled_tiles.append(primary)
            motif_details[primary.content_hash] = getattr(primary, "motif_annotations")

        assembled_tiles.sort(key=lambda t: float(t.score or 0.0), reverse=True)
        timings["stage4_ms"] = round((time.monotonic() - stage4_t0) * 1000, 1)

        stage5_t0 = time.monotonic()
        deduped: List[TileResult] = []
        seen_hashes = set()
        for tile in assembled_tiles:
            if not tile.content_hash or tile.content_hash in seen_hashes:
                continue
            seen_hashes.add(tile.content_hash)
            deduped.append(tile)
            if len(deduped) >= top_k:
                break
        timings["stage5_ms"] = round((time.monotonic() - stage5_t0) * 1000, 1)

        return {
            "query": query,
            "tiles": deduped,
            "total_tokens": sum(t.token_count for t in deduped),
            "hmm_reranked": reranker_used,
            "version": "composed_v1",
            "diagnostics": {
                "candidate_count": len(candidate_state),
                "seed_count": len(search_result.tiles),
                "graph_neighbors": graph_neighbors,
                "context_escalated": len(context_tiles),
                "full_escalated": len(full_tiles),
                "reranked_count": len(reranked_tiles),
                "candidate_hashes": [s["content_hash"] for s in ranked_pool[: fetch_k]],
                "motif_annotations": motif_details,
                "stage_timings_ms": timings,
                "total_pipeline_ms": round((time.monotonic() - t0) * 1000, 1),
                "latency_budget_ms": 700,
            },
        }

    def _fetch_overlap_tiles(
        self,
        seeds: List[TileResult],
        scale: str,
        batch_size: int = 10,
    ) -> Dict[str, TileResult]:
        """Find overlapping parent tiles at a larger scale via batched Weaviate filters."""
        if not seeds:
            return {}

        matches: Dict[str, TileResult] = {}
        for i in range(0, len(seeds), batch_size):
            batch = [s for s in seeds[i:i + batch_size] if s.source_file]
            if not batch:
                continue

            operands = []
            for seed in batch:
                operands.append(
                    "{ operator: And, operands: ["
                    f'{{ path: ["source_file"], operator: Equal, valueText: "{_escape_gql(seed.source_file)}" }}, '
                    f'{{ path: ["scale"], operator: Equal, valueText: "{scale}" }}, '
                    f'{{ path: ["start_char"], operator: LessThanEqual, valueInt: {int(seed.end_char or 0)} }}, '
                    f'{{ path: ["end_char"], operator: GreaterThanEqual, valueInt: {int(seed.start_char or 0)} }}'
                    "] }"
                )

            gql = (
                f"{{ Get {{ {V1_CLASS}("
                f"where: {{ operator: Or, operands: [{', '.join(operands)}] }}"
                f" limit: {max(len(batch) * 4, 4)}"
                f") {{ {STAGE_TILE_PROPS} _additional {{ id score }} }} }} }}"
            )
            data = _graphql(gql)
            objs = data.get("data", {}).get("Get", {}).get(V1_CLASS, [])
            parsed_tiles = [_parse_tile(obj) for obj in objs]

            for seed in batch:
                best_tile = None
                best_key = None
                for tile in parsed_tiles:
                    if tile.source_file != seed.source_file:
                        continue
                    if tile.end_char < seed.start_char or tile.start_char > seed.end_char:
                        continue
                    overlap = min(tile.end_char, seed.end_char) - max(tile.start_char, seed.start_char)
                    contains = tile.start_char <= seed.start_char and tile.end_char >= seed.end_char
                    key = (1 if contains else 0, overlap, tile.token_count)
                    if best_key is None or key > best_key:
                        best_key = key
                        best_tile = tile
                if best_tile is not None:
                    matches[seed.content_hash] = best_tile
        return matches

    def _normalize_tile_scores(self, tiles: List[TileResult]) -> Dict[str, float]:
        """Normalize tile scores into the 0..1 range."""
        if not tiles:
            return {}
        raw = [float(t.score or 0.0) for t in tiles]
        lo, hi = min(raw), max(raw)
        if hi > lo:
            return {
                tile.content_hash: (float(tile.score or 0.0) - lo) / (hi - lo)
                for tile in tiles if tile.content_hash
            }
        denom = max(len(tiles) - 1, 1)
        return {
            tile.content_hash: 1.0 - (idx / denom)
            for idx, tile in enumerate(tiles) if tile.content_hash
        }

    def _motif_overlap(
        self,
        query_motifs: List[str],
        tile_motifs: List[str],
    ) -> Tuple[float, List[str]]:
        """Soft motif overlap score for rerank fusion."""
        query_set = {m.upper() for m in query_motifs if m}
        tile_set = {m.upper() for m in tile_motifs if m}
        if not query_set or not tile_set:
            return 0.0, []
        matched = sorted(query_set & tile_set)
        union = query_set | tile_set
        return len(matched) / max(len(union), 1), matched

    def _recency_score(self, loaded_at: str) -> float:
        """Recent tiles get a small bonus; malformed timestamps stay neutral."""
        if not loaded_at:
            return 0.5
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(loaded_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max((datetime.now(timezone.utc) - dt).total_seconds() / 86400.0, 0.0)
            return 1.0 / (1.0 + (age_days / 180.0))
        except Exception:
            return 0.5

    def _build_multi_scale_payload(
        self,
        search_tile: Optional[TileResult],
        context_tile: Optional[TileResult],
        full_tile: Optional[TileResult],
    ) -> Dict[str, dict]:
        """Serialize multi-scale tiles for API consumers."""
        payload = {}
        for label, tile in (
            ("search_512", search_tile),
            ("context_2048", context_tile),
            ("full_4096", full_tile),
        ):
            if tile is None:
                continue
            payload[label] = {
                "content_hash": tile.content_hash,
                "scale": tile.scale,
                "content": tile.content,
                "source_file": tile.source_file,
                "start_char": tile.start_char,
                "end_char": tile.end_char,
                "score": round(float(tile.score or 0.0), 4),
            }
        return payload

    def _v1_plus_search(
        self,
        query: str,
        top_k: int,
        instruction: str,
        query_type: str,
        v1,
        alpha: Optional[float] = None,
        expand_graph: bool = True,
        graph_depth: int = 2,
        semantic_query: Optional[str] = None,
        **filters,
    ) -> Dict[str, Any]:
        """V1 hybrid search + lazy V2 metadata overlay before reranking.

        Replaces Option E (V2 rosetta nearVector path) as the default non-relational
        strategy. V2 metadata (richer doc-level rosetta_summary, dominant_motifs)
        is overlaid onto V1 passage tiles before the cross-encoder sees them,
        improving reranker quality without the latency of V2 vector search.

        Sequence:
          1. V1 search (nearVector + BM25, 3x candidate pool, query-aware alpha)
          2. Lazy V2 metadata overlay (batch-fetch by content_hash OR-filter)
          3. Qwen3-Reranker-8B cross-encoder on enriched candidates
          4. Dedup by content_hash + truncate to top_k
          5. Optional graph expansion payload via V1 Neo4j RELATES_TO edges
        """
        from dataclasses import replace as dc_replace

        fetch_k = top_k * 3

        # 6C.2: Dynamic alpha — base band adjusted by stop-word ratio for exact queries
        # Queries with high stop-word content (e.g., "SOUL equals INFRA equals...") need
        # more vector weight since BM25 is polluted by common words.
        _stop_ratio = 0.0
        if alpha is None:
            alpha = ALPHA_BANDS.get(query_type, ALPHA_BANDS["default"])
            if query_type == "exact":
                import re
                _STOP = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
                         'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
                         'from', 'as', 'into', 'about', 'equals', 'equal',
                         'divided', 'between', 'and', 'or', 'not', 'but'}
                words = re.findall(r'[a-zA-Z]+', query.lower())
                if words:
                    _stop_ratio = sum(1 for w in words if w in _STOP) / len(words)
                    # Blend: alpha goes from 0.3 (0% stops) to 0.65 (50%+ stops)
                    alpha = 0.3 + _stop_ratio * 0.7
                    alpha = min(alpha, 0.65)

        # Step 1: V1 vector + BM25 search with query-aware alpha
        # 6C.1: Pass semantic_query as vector_query for temporal token stripping
        _t_v1 = time.monotonic()
        search_result = v1.search(
            query, top_k=fetch_k, expand_parents=True, alpha=alpha,
            vector_query=semantic_query,
            **filters,
        )
        _t_v1_ms = (time.monotonic() - _t_v1) * 1000
        if not search_result.tiles:
            return {
                "query": query, "tiles": [], "total_tokens": 0,
                "hmm_reranked": False, "version": "v1plus",
                "diagnostics": {"candidate_count": 0},
            }

        # 6C.2+6C.4: Supplementary lanes run IN PARALLEL (BM25, ColBERT, rosetta)
        # Previously sequential — parallelizing saves ~1-3s on exact queries.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        bm25_lane_count = 0
        colbert_lane_count = 0
        rosetta_lane_count = 0
        _t_lanes = time.monotonic()

        def _bm25_lane():
            """BM25 supplementary for exact queries — keyword-only matches."""
            try:
                return v1.search_bm25(query, top_k=fetch_k, **filters)
            except Exception as e:
                log.debug("BM25 lane failed: %s", e)
                return None

        def _colbert_lane():
            """ColBERT MuVera for exact queries — multi-vector token matching."""
            try:
                hashes = self._search_colbert(query, top_k=fetch_k)
                if hashes:
                    return hashes
            except Exception as e:
                log.debug("ColBERT lane failed: %s", e)
            return None

        def _rosetta_lane():
            """V2 rosetta for conceptual queries — semantic summary matching."""
            try:
                return self.search_rosetta(query, top_k=fetch_k // 2)
            except Exception as e:
                log.debug("Rosetta lane failed: %s", e)
            return None

        # Dispatch parallel lanes based on query type
        lane_futures = {}
        with ThreadPoolExecutor(max_workers=3) as lane_pool:
            if query_type == "exact":
                lane_futures["bm25"] = lane_pool.submit(_bm25_lane)
                if _colbert_collection_exists():
                    lane_futures["colbert"] = lane_pool.submit(_colbert_lane)
            elif query_type == "conceptual" and self.is_available():
                lane_futures["rosetta"] = lane_pool.submit(_rosetta_lane)

            # Collect results
            bm25_result = None
            colbert_hashes = None
            rosetta_results = None

            for name, fut in lane_futures.items():
                try:
                    r = fut.result(timeout=15)
                    if name == "bm25":
                        bm25_result = r
                    elif name == "colbert":
                        colbert_hashes = r
                    elif name == "rosetta":
                        rosetta_results = r
                except Exception as e:
                    log.debug("Lane %s timed out or failed: %s", name, e)

        # Merge BM25 results
        if bm25_result and bm25_result.tiles:
            existing_hashes = {t.content_hash for t in search_result.tiles if t.content_hash}
            new_bm25_tiles = [
                t for t in bm25_result.tiles
                if t.content_hash and t.content_hash not in existing_hashes
            ]
            if new_bm25_tiles:
                bm25_lane_count = len(new_bm25_tiles)
                merged = list(search_result.tiles) + new_bm25_tiles[:10]
                search_result = SearchResult(
                    query=search_result.query, tiles=merged,
                    total_tokens=sum(t.token_count for t in merged),
                    search_time_ms=search_result.search_time_ms,
                )

        # Merge ColBERT results
        if colbert_hashes:
            existing_hashes = {t.content_hash for t in search_result.tiles if t.content_hash}
            new_hashes = [h for h in colbert_hashes if h not in existing_hashes]
            if new_hashes:
                colbert_tiles = self._fetch_v1_tiles_by_hash(new_hashes[:10], v1)
                if colbert_tiles:
                    colbert_lane_count = len(colbert_tiles)
                    merged = list(search_result.tiles) + colbert_tiles
                    search_result = SearchResult(
                        query=search_result.query, tiles=merged,
                        total_tokens=sum(t.token_count for t in merged),
                        search_time_ms=search_result.search_time_ms,
                    )

        # Merge rosetta results
        if rosetta_results:
            existing_hashes = {t.content_hash for t in search_result.tiles if t.content_hash}
            new_hashes = []
            for obj, score in rosetta_results:
                ch = obj.get("content_hash", "")
                if ch and ch not in existing_hashes:
                    new_hashes.append(ch)
            if new_hashes:
                rosetta_tiles = self._fetch_v1_tiles_by_hash(new_hashes[:15], v1)
                if rosetta_tiles:
                    rosetta_lane_count = len(rosetta_tiles)
                    merged_tiles = list(search_result.tiles) + rosetta_tiles
                    search_result = SearchResult(
                        query=search_result.query,
                        tiles=merged_tiles,
                        total_tokens=sum(t.token_count for t in merged_tiles),
                        search_time_ms=search_result.search_time_ms,
                    )

        _t_lanes_ms = (time.monotonic() - _t_lanes) * 1000

        # 6C.3: Asymmetric quota — cap candidate pool at fetch_k before reranker.
        # Supplementary lanes add tiles V1 missed (valuable), growing pool to 40-50.
        # The reranker scales ~200ms/doc under GPU contention, so keep pool bounded.
        # Strategy: keep ALL supplementary tiles (they were specifically found),
        # truncate low-ranked V1 tiles to make room.
        _supplementary_count = bm25_lane_count + colbert_lane_count + rosetta_lane_count
        if len(search_result.tiles) > fetch_k and _supplementary_count > 0:
            # V1 tiles are first, supplementary tiles appended at end
            v1_count = len(search_result.tiles) - _supplementary_count
            v1_keep = max(fetch_k - _supplementary_count, top_k)  # keep at least top_k V1
            capped = search_result.tiles[:v1_keep] + search_result.tiles[v1_count:]
            capped = capped[:fetch_k]
            search_result = SearchResult(
                query=search_result.query,
                tiles=capped,
                total_tokens=sum(t.token_count for t in capped),
                search_time_ms=search_result.search_time_ms,
            )

        # 6C.0 Instrumentation: capture pre-rerank candidate pool
        pre_rerank_hashes = [t.content_hash for t in search_result.tiles if t.content_hash]
        # Oracle recall: text snippets of ALL candidates (for benchmark to check gold terms)
        _pre_rerank_texts = []
        for t in search_result.tiles:
            text = (t.content or "")[:1000]
            if t.rosetta_summary:
                text += " " + t.rosetta_summary[:500]
            if t.dominant_motifs:
                text += " " + " ".join(t.dominant_motifs)
            _pre_rerank_texts.append(text)

        # Step 2: Lazy V2 metadata overlay
        _t_v2 = time.monotonic()
        content_hashes = [t.content_hash for t in search_result.tiles if t.content_hash]
        v2_overlay_count = 0
        if content_hashes:
            v2_meta = self._fetch_v2_metadata(content_hashes)
            if v2_meta:
                v2_overlay_count = len(v2_meta)
                enriched = []
                for tile in search_result.tiles:
                    ch = tile.content_hash
                    if ch and ch in v2_meta:
                        meta = v2_meta[ch]
                        tile = dc_replace(
                            tile,
                            rosetta_summary=meta.get("rosetta_summary") or tile.rosetta_summary,
                            dominant_motifs=meta.get("dominant_motifs") or tile.dominant_motifs,
                        )
                    enriched.append(tile)
                search_result = SearchResult(
                    query=search_result.query,
                    tiles=enriched,
                    total_tokens=search_result.total_tokens,
                    search_time_ms=search_result.search_time_ms,
                )

        _t_v2_ms = (time.monotonic() - _t_v2) * 1000

        # Phase 8B: Dedup by content_hash BEFORE reranker to maximize unique
        # candidates. When multiple scales exist for the same hash, prefer
        # search_512 (most specific) to avoid broad context_2048/full_4096
        # tiles crowding out specific passages.
        SCALE_PRIORITY = {"search_512": 0, "context_2048": 1, "full_4096": 2, "rosetta": 3}
        best_per_hash: dict = {}
        for tile in search_result.tiles:
            ch = tile.content_hash or str(id(tile))
            existing = best_per_hash.get(ch)
            if existing is None:
                best_per_hash[ch] = tile
            else:
                # Prefer search_512 scale, then higher score
                tile_pri = SCALE_PRIORITY.get(tile.scale, 9)
                existing_pri = SCALE_PRIORITY.get(existing.scale, 9)
                if tile_pri < existing_pri or (tile_pri == existing_pri and float(tile.score or 0) > float(existing.score or 0)):
                    best_per_hash[ch] = tile
        deduped_pre_rerank = list(best_per_hash.values())
        # Sort by original score to maintain ranking (cast to float for safety)
        deduped_pre_rerank.sort(key=lambda t: float(t.score) if t.score is not None else 0.0, reverse=True)
        log.info("Pre-rerank dedup: %d -> %d unique hashes", len(search_result.tiles), len(deduped_pre_rerank))

        # 6C p95: Data-driven rerank_k per query type (oracle recall validated).
        RERANK_K = {
            "exact": 30,
            "temporal": 25,
            "conceptual": 30,
            "relational": 22,
            "memory": 40,
            "humor": 40,
        }
        rerank_k = RERANK_K.get(query_type, 30)
        rerank_candidates = deduped_pre_rerank[:rerank_k]
        search_result = SearchResult(
            query=search_result.query,
            tiles=rerank_candidates,
            total_tokens=sum(t.token_count for t in rerank_candidates),
            search_time_ms=search_result.search_time_ms,
        )

        # Step 3: Neural rerank (Qwen3-Reranker-8B)
        _t_rerank = time.monotonic()
        reranked_result = v1.hmm_rerank(
            search_result, query, query_type=query_type, instruction=instruction,
        )
        _t_rerank_ms = (time.monotonic() - _t_rerank) * 1000

        # Step 4: Dedup by content_hash
        seen: set = set()
        deduped = []
        for tile in reranked_result.tiles:
            key = tile.content_hash or id(tile)
            if key not in seen:
                seen.add(key)
                deduped.append(tile)

        tiles = deduped[:top_k]

        # Phase 8B: Provenance-Weighted Scoring (replaces simple coherence boost)
        try:
            from isma.src.provenance_scorer import apply_provenance_scoring
            tiles = apply_provenance_scoring(
                tiles, query_type=query_type,
            )
        except Exception as e:
            log.warning("Provenance scoring failed, falling back to unscored: %s", e)

        # 6C.0 Instrumentation: diagnostics for benchmark oracle recall + timing
        post_rerank_hashes = [t.content_hash for t in tiles if t.content_hash]
        diagnostics = {
            "candidate_count": len(pre_rerank_hashes),
            "candidate_hashes": pre_rerank_hashes,
            "candidate_texts": _pre_rerank_texts,
            "v2_overlay_count": v2_overlay_count,
            "reranked_count": len(post_rerank_hashes),
            "reranked_hashes": post_rerank_hashes,
            "alpha": alpha,
            "rosetta_lane_count": rosetta_lane_count,
            "bm25_lane_count": bm25_lane_count,
            "colbert_lane_count": colbert_lane_count,
            "graph_expansion_count": 0,
            "timing_ms": {
                "v1_search": round(_t_v1_ms, 1),
                "supplementary_lanes": round(_t_lanes_ms, 1),
                "v2_overlay": round(_t_v2_ms, 1),
                "reranker": round(_t_rerank_ms, 1),
            },
        }

        result = {
            "query": query,
            "tiles": tiles,
            "total_tokens": sum(t.token_count for t in tiles),
            "hmm_reranked": True,
            "version": "v1plus",
            "graph_expansions": {},
            "diagnostics": diagnostics,
        }

        if expand_graph:
            graph_expansions = {}
            for tile in tiles:
                if tile.hmm_enriched and tile.content_hash:
                    expansion = v1.graph_expand(tile.content_hash, depth=graph_depth)
                    if expansion.related_tiles:
                        graph_expansions[tile.content_hash] = {
                            "related": expansion.related_tiles,
                            "depth": expansion.depth,
                        }
            result["graph_expansions"] = graph_expansions
            result["diagnostics"]["graph_expansion_count"] = len(graph_expansions)

        return result

    # ── Relational Cascade (Dream Cycle Architecture) ────────────

    @staticmethod
    def _relational_concepts_usable(sub_queries: List[str]) -> bool:
        """Check if decomposed concepts are specific enough for cascade.

        Generic concepts ('platforms', 'early to recent conversations')
        cause the relational reranker to demote gold tiles — these queries
        perform better via V1-Plus. Requires both concepts to be substantive.
        """
        GENERIC_TERMS = {
            "platforms", "platform", "ai family members", "family members",
            "sessions", "conversations", "discussions", "messages",
            "early to recent conversations", "recent conversations",
            "different ai family members", "ai family",
        }
        for concept in sub_queries[:2]:
            c = concept.lower().strip()
            # Too short to be a real concept
            if len(c) < 6:
                return False
            # Matches a known generic term
            if c in GENERIC_TERMS:
                return False
            # Strip articles and check
            c_stripped = c.removeprefix("the ").removeprefix("a ")
            if c_stripped in GENERIC_TERMS:
                return False
        return True

    def _search_relational_cascade(
        self,
        query: str,
        sub_queries: List[str],
        top_k: int,
        instruction: str,
        v1,
        **filters,
    ) -> Dict[str, Any]:
        """Bi-Substrate Cascading Membrane for relational queries.

        Runs the relational cascade (Alpha→Beta→Gamma) IN PARALLEL with
        V1-Plus baseline. V1-Plus is the BASE (all its tiles reach reranker
        to prevent regression). Cascade tiles are SUPPLEMENTAL candidates
        that can only improve results, never displace known-good V1 tiles.

        Sequence:
          1. Parallel: relational cascade + V1-Plus baseline
          2. Interleaved pool: all V1-Plus + top cascade supplement
          3. Backfill content for cascade-only tiles
          4. Relational-specific reranker prompt
          5. Provenance scoring + dedup
        """
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace as dc_replace
        from isma.src.relational_retrieval import search_relational

        obj_map: Dict[str, TileResult] = {}

        # Step 1: Run cascade + V1-Plus in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            cascade_fut = pool.submit(
                search_relational, query, sub_queries, top_k,
            )
            # Request wider V1-Plus pool — cascade does its own reranking,
            # so we need more candidates than just top_k.
            v1plus_fut = pool.submit(
                self._v1_plus_search,
                query, top_k * 3,
                instruction=instruction,
                query_type="relational",
                v1=v1,
                **filters,
            )

            # Collect cascade results
            cascade_result = {}
            cascade_hashes = []
            try:
                cascade_result = cascade_fut.result(timeout=25)
                cascade_hashes = cascade_result.get("hashes", [])
            except Exception as e:
                log.warning("Relational cascade failed: %s", e)

            # Collect V1-Plus results — these are the BASE (must not regress)
            v1plus_tiles = []
            v1plus_hashes = []
            try:
                v1plus_result = v1plus_fut.result(timeout=25)
                for tile in v1plus_result.get("tiles", []):
                    ch = tile.content_hash
                    if ch:
                        v1plus_tiles.append(tile)
                        v1plus_hashes.append(ch)
                        if ch not in obj_map:
                            obj_map[ch] = tile
            except Exception as e:
                log.warning("V1-Plus relational baseline failed: %s", e)

        if not cascade_hashes and not v1plus_hashes:
            return {
                "query": query, "tiles": [], "total_tokens": 0,
                "hmm_reranked": False, "version": "relational_cascade",
                "diagnostics": {"state": "failed"},
            }

        # Step 2: Interleaved pool — V1-Plus base + cascade supplement
        # V1-Plus tiles ALWAYS make it to reranker (prevents regression).
        # Cascade tiles are added as new candidates (enables improvement).
        v1_set = set(v1plus_hashes)
        cascade_supplement = [ch for ch in cascade_hashes if ch not in v1_set]
        # Interleave: all V1-Plus + top cascade supplement tiles
        candidate_hashes = v1plus_hashes + cascade_supplement[:top_k * 4]

        # Step 3: Backfill tile content for cascade-only results (no V1 tile yet)
        missing_hashes = [ch for ch in candidate_hashes if ch not in obj_map]
        if missing_hashes:
            backfilled = self._fetch_v1_tiles_by_hash(missing_hashes[:50], v1)
            for tile in backfilled:
                if tile.content_hash and tile.content_hash not in obj_map:
                    obj_map[tile.content_hash] = tile

            # If still missing after V1 fetch, try V2
            still_missing = [ch for ch in missing_hashes if ch not in obj_map]
            if still_missing:
                v2_meta = self._fetch_v2_metadata(still_missing[:40])
                for ch, meta in v2_meta.items():
                    if ch not in obj_map:
                        obj_map[ch] = _v2_to_tile(meta, score=0.0)

        # Build candidate pool — V1-Plus tiles keep original scores, cascade tiles get 0
        tiles = []
        for ch in candidate_hashes:
            if ch in obj_map:
                tiles.append(obj_map[ch])

        if not tiles:
            return {
                "query": query, "tiles": [], "total_tokens": 0,
                "hmm_reranked": False, "version": "relational_cascade",
                "diagnostics": cascade_result.get("diagnostics", {}),
            }

        # Step 4: V2 metadata overlay for richer rosetta/motifs
        v2_meta = self._fetch_v2_metadata([t.content_hash for t in tiles if t.content_hash])
        if v2_meta:
            tiles = [
                dc_replace(
                    t,
                    rosetta_summary=v2_meta[t.content_hash].get("rosetta_summary") or t.rosetta_summary,
                    dominant_motifs=v2_meta[t.content_hash].get("dominant_motifs") or t.dominant_motifs,
                ) if t.content_hash in v2_meta else t
                for t in tiles
            ]

        # Dedup before reranker
        seen: set = set()
        deduped = []
        for tile in tiles:
            key = tile.content_hash or id(tile)
            if key not in seen:
                seen.add(key)
                deduped.append(tile)

        # Step 5: Relational-specific reranker with custom prompt
        # Pool: V1-Plus base (~10-20 tiles) + cascade supplement (~30-40 tiles)
        rerank_pool = deduped[:60]
        search_for_rerank = SearchResult(
            query=query,
            tiles=rerank_pool,
            total_tokens=sum(t.token_count for t in rerank_pool),
            search_time_ms=0,
        )

        # Extract concepts for relational reranker instruction
        concept_a = sub_queries[0] if len(sub_queries) > 0 else ""
        concept_b = sub_queries[1] if len(sub_queries) > 1 else ""
        relational_instruction = (
            f"Does this passage discuss the relationship between "
            f"{concept_a} and {concept_b}? "
            f"Prioritize passages where both concepts co-occur and interact."
        )

        try:
            reranked = v1.hmm_rerank(
                search_for_rerank, query,
                query_type="relational",
                instruction=relational_instruction,
            )
            result_tiles = reranked.tiles[:top_k]
        except Exception as e:
            log.warning("Relational reranker failed: %s", e)
            result_tiles = rerank_pool[:top_k]

        # Step 6: Provenance scoring
        try:
            from isma.src.provenance_scorer import apply_provenance_scoring
            result_tiles = apply_provenance_scoring(
                result_tiles, query_type="relational",
            )
        except Exception as e:
            log.warning("Provenance scoring failed (relational): %s", e)

        # Diagnostics
        cascade_diag = cascade_result.get("diagnostics", {})
        diagnostics = {
            "cascade_state": cascade_diag.get("state", "unknown"),
            "cascade_concept_a": cascade_diag.get("concept_a", ""),
            "cascade_concept_b": cascade_diag.get("concept_b", ""),
            "cascade_motif_a": cascade_diag.get("motif_a"),
            "cascade_motif_b": cascade_diag.get("motif_b"),
            "cascade_result_count": cascade_diag.get("result_count", 0),
            "v1plus_candidate_count": len(v1plus_hashes),
            "rrf_pool_size": len(candidate_hashes),
            "reranked_count": len(result_tiles),
            "reranked_hashes": [t.content_hash for t in result_tiles if t.content_hash],
            "candidate_hashes": candidate_hashes,
        }

        return {
            "query": query,
            "tiles": result_tiles,
            "total_tokens": sum(t.token_count for t in result_tiles),
            "hmm_reranked": True,
            "version": "relational_cascade",
            "diagnostics": diagnostics,
        }

    def _search_motif(
        self,
        query: str,
        detected_motifs: List[str],
        top_k: int,
        instruction: str,
        v1,
        **filters,
    ) -> Dict[str, Any]:
        """Motif-aware search: RRF of V1 hybrid + Redis/Neo4j motif candidates.

        Wires detected_motifs into actual retrieval, replacing the previous
        hybrid_search() call that ignored motifs entirely (R@10=0.000).

        Sequence:
          1a. V1 nearVector+BM25 with dominant_motifs Weaviate pre-filter (base signal)
          1b. Redis inverted index → Neo4j amplitude sort per detected motif (1.5x weight)
          2.  RRF merge of all paths
          3.  Lazy V2 metadata overlay on candidate pool
          4.  Backfill passage content for motif-only tiles
          5.  Qwen3-Reranker-8B rerank + dedup
        """
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace as dc_replace

        k = 60
        rrf_scores: Dict[str, float] = {}
        obj_map: Dict[str, TileResult] = {}

        # Step 1: Run V1 hybrid (motif-filtered) + motif searches in parallel
        motif_filters = dict(filters, dominant_motifs=detected_motifs)
        with ThreadPoolExecutor(max_workers=min(len(detected_motifs) + 1, MAX_THREAD_FANOUT)) as executor:
            hybrid_fut = executor.submit(
                v1.search, query, top_k=top_k * 3, expand_parents=True, **motif_filters
            )
            motif_futs = {
                m: executor.submit(v1.motif_search, m, limit=top_k * 3)
                for m in detected_motifs
            }

            # Collect V1 hybrid results (base weight = 1.0)
            try:
                hybrid_result = hybrid_fut.result(timeout=20)
                for rank, tile in enumerate(hybrid_result.tiles):
                    ch = tile.content_hash
                    if ch:
                        rrf_scores[ch] = rrf_scores.get(ch, 0) + 1.0 / (k + rank + 1)
                        obj_map[ch] = tile
            except Exception as e:
                log.warning("Motif hybrid search failed: %s", e)

            # Collect Redis+Neo4j motif results (boosted weight = 1.5, amplitude-sorted)
            for motif_id, fut in motif_futs.items():
                try:
                    msr = fut.result(timeout=10)
                    for rank, tw in enumerate(msr.tiles_with_amplitude):
                        ch = tw.get("tile_hash", "")
                        if ch:
                            rrf_scores[ch] = rrf_scores.get(ch, 0) + 1.5 / (k + rank + 1)
                            if ch not in obj_map:
                                obj_map[ch] = TileResult(
                                    content="",
                                    score=0.0,
                                    tile_id=ch,
                                    scale="search_512",
                                    source_type="",
                                    source_file="",
                                    content_hash=ch,
                                    platform=tw.get("platform", ""),
                                    rosetta_summary=tw.get("rosetta_summary", ""),
                                    dominant_motifs=tw.get("dominant_motifs") or [],
                                )
                except Exception as e:
                    log.warning("Motif search %s failed: %s", motif_id, e)

        if not rrf_scores:
            # All paths failed — fall back to plain V1-Plus
            return self._v1_plus_search(
                query, top_k, instruction=instruction, query_type="motif", v1=v1, **filters
            )

        # 6C.0 Instrumentation: track per-lane contributions
        hybrid_lane_hashes = set()
        motif_lane_hashes: Dict[str, set] = {}  # motif_id → set of hashes
        # Re-scan to attribute lanes (rrf_scores already accumulated)
        try:
            hybrid_result_tiles = hybrid_fut.result(timeout=0)  # already resolved
            hybrid_lane_hashes = {t.content_hash for t in hybrid_result_tiles.tiles if t.content_hash}
        except Exception:
            pass
        for motif_id, fut in motif_futs.items():
            try:
                msr = fut.result(timeout=0)
                motif_lane_hashes[motif_id] = {
                    tw.get("tile_hash", "") for tw in msr.tiles_with_amplitude if tw.get("tile_hash")
                }
            except Exception:
                pass

        # Step 2: Sort by RRF and take candidate pool
        sorted_hashes = sorted(rrf_scores, key=lambda ch: rrf_scores[ch], reverse=True)
        candidate_hashes = sorted_hashes[:top_k * 2]

        # Step 3: Lazy V2 metadata overlay
        v2_meta = self._fetch_v2_metadata([ch for ch in candidate_hashes if ch])
        tiles = []
        for ch in candidate_hashes:
            if ch not in obj_map:
                continue
            tile = obj_map[ch]
            if ch in v2_meta:
                meta = v2_meta[ch]
                tile = dc_replace(
                    tile,
                    rosetta_summary=meta.get("rosetta_summary") or tile.rosetta_summary,
                    dominant_motifs=meta.get("dominant_motifs") or tile.dominant_motifs,
                )
            tiles.append(dc_replace(tile, score=rrf_scores[ch]))

        # Step 4: Backfill passage content for motif-only tiles (no V1 content)
        backfill_map: Dict[str, TileResult] = {
            t.content_hash: t for t in tiles if not t.content and t.content_hash
        }
        if backfill_map:
            self._fill_content(list(backfill_map.keys()), backfill_map)
            tiles = [
                backfill_map.get(t.content_hash, t) if not t.content and t.content_hash else t
                for t in tiles
            ]

        # Step 5: Skip reranker for motif queries (6C p95 consultation — unanimous)
        # R@10=0.930 without reranker. Domain-specific routing (Redis inverted index +
        # Neo4j amplitude sort + RRF fusion) IS the reranker for motif. Neural re-evaluation
        # of graph-proven amplitudes wastes ~5000ms for zero recall benefit.
        # RRF score ordering is sufficient.

        # Dedup + truncate
        seen: set = set()
        deduped = []
        for tile in tiles:
            key = tile.content_hash or id(tile)
            if key not in seen:
                seen.add(key)
                deduped.append(tile)

        result_tiles = deduped[:top_k]

        # Phase 8B: Provenance-Weighted Scoring
        try:
            from isma.src.provenance_scorer import apply_provenance_scoring
            result_tiles = apply_provenance_scoring(
                result_tiles, query_type="motif",
            )
        except Exception as e:
            log.warning("Provenance scoring failed (motif path): %s", e)

        # 6C.0 Instrumentation: motif search diagnostics
        all_motif_hashes = set()
        for mh in motif_lane_hashes.values():
            all_motif_hashes |= mh
        diagnostics = {
            "candidate_count": len(candidate_hashes),
            "candidate_hashes": candidate_hashes,
            "hybrid_lane_count": len(hybrid_lane_hashes),
            "motif_lane_count": len(all_motif_hashes),
            "motif_lane_detail": {m: len(h) for m, h in motif_lane_hashes.items()},
            "reranked_count": len(result_tiles),
            "reranked_hashes": [t.content_hash for t in result_tiles if t.content_hash],
        }

        return {
            "query": query,
            "tiles": result_tiles,
            "total_tokens": sum(t.token_count for t in result_tiles),
            "hmm_reranked": True,
            "version": "v1plus_motif",
            "diagnostics": diagnostics,
        }

    def _fetch_v2_metadata(self, content_hashes: List[str]) -> Dict[str, dict]:
        """Batch-fetch rosetta_summary and dominant_motifs from V2 by content_hash.

        Uses OR-filter batches of 50 to stay within GraphQL complexity limits.
        Returns dict: content_hash → {rosetta_summary, dominant_motifs}
        """
        result: Dict[str, dict] = {}
        batch_size = 50
        for i in range(0, len(content_hashes), batch_size):
            batch = content_hashes[i:i + batch_size]
            if len(batch) == 1:
                where = (
                    f'{{ path: ["content_hash"], operator: Equal, '
                    f'valueText: "{_escape_gql(batch[0])}" }}'
                )
            else:
                operands = ", ".join(
                    f'{{ path: ["content_hash"], operator: Equal, valueText: "{_escape_gql(ch)}" }}'
                    for ch in batch
                )
                where = f'{{ operator: Or, operands: [{operands}] }}'
            q = (
                f'{{ Get {{ {V2_CLASS}('
                f'where: {where}'
                f' limit: {len(batch)}'
                f') {{ content_hash rosetta_summary dominant_motifs }} }} }}'
            )
            try:
                data = _graphql(q)
                for obj in data.get("data", {}).get("Get", {}).get(V2_CLASS, []):
                    ch = obj.get("content_hash", "")
                    if ch:
                        result[ch] = obj
            except Exception as e:
                log.debug("V2 metadata fetch failed for batch %d: %s", i // batch_size, e)
        return result

    def _search_relational(
        self,
        query: str,
        sub_queries: list,
        top_k: int = 10,
        instruction: str = "",
        expand_graph: bool = True,
        graph_depth: int = 2,
        **filters,
    ) -> Dict[str, Any]:
        """Relational search: sub-queries + graph expansion merged via RRF."""
        t0 = time.monotonic()
        k = 60  # RRF constant

        rrf_scores: Dict[str, float] = {}
        obj_map: Dict[str, Any] = {}

        # Run sub-queries + full query in parallel (full query gets 2x weight)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        all_queries = [(sq, 1.0) for sq in sub_queries] + [(query, 2.0)]
        with ThreadPoolExecutor(max_workers=min(len(all_queries), MAX_THREAD_FANOUT)) as executor:
            futures = {
                executor.submit(self.search, q, top_k=top_k * 2, **filters): weight
                for q, weight in all_queries
            }
            for future in as_completed(futures):
                weight = futures[future]
                try:
                    result = future.result()
                    for rank, tile in enumerate(result.tiles):
                        ch = tile.content_hash
                        if ch:
                            rrf_scores[ch] = rrf_scores.get(ch, 0) + weight / (k + rank + 1)
                            obj_map[ch] = tile
                except Exception as e:
                    log.warning("Relational sub-query failed: %s", e)

        # Phase 4: Neo4j graph expansion from seed results
        graph_expanded = 0
        if expand_graph:
            try:
                from isma.src.hmm.neo4j_store import HMMNeo4jStore
                store = HMMNeo4jStore()  # Uses shared driver singleton
                # Use top-ranked seed tiles for graph expansion (sorted by RRF score)
                seed_ids = sorted(rrf_scores.keys(), key=lambda ch: rrf_scores[ch], reverse=True)[:top_k]
                if seed_ids:
                    neighbors = store.graph_expand(
                        seed_ids, depth=graph_depth, follow_supersedes=True,
                    )
                    for rank, nb in enumerate(neighbors):
                        ch = nb.get("content_hash", "")
                        if ch:
                            # Accumulate RRF unconditionally — previously skipped nodes already
                            # found by direct search, penalizing the highest-relevance items.
                            rrf_scores[ch] = rrf_scores.get(ch, 0) + 0.5 / (k + rank + 1)
                            if ch not in obj_map:
                                # Create TileResult from graph data (only if not already present)
                                obj_map[ch] = TileResult(
                                    content="",  # Content fetched later if needed
                                    content_hash=ch,
                                    platform=nb.get("platform", ""),
                                    source_type="", source_file="",
                                    session_id="", document_id="",
                                    scale="canonical", tile_id=ch,
                                    token_count=0, score=rrf_scores[ch],
                                    rosetta_summary=nb.get("rosetta_summary", ""),
                                    dominant_motifs=nb.get("dominant_motifs") or [],
                                )
                                graph_expanded += 1
                # No store.close() needed — uses shared driver singleton
            except Exception as e:
                log.debug("Graph expansion failed: %s", e)

        # Batch-fetch content for graph-expanded tiles (content="")
        empty_hashes = [
            ch for ch, tile in obj_map.items()
            if not tile.content and ch in rrf_scores
        ]
        if empty_hashes:
            self._fill_content(empty_hashes, obj_map)

        # Sort by RRF score and take top_k (immutable — use dataclass replace)
        from dataclasses import replace as dc_replace
        sorted_hashes = sorted(rrf_scores.keys(), key=lambda ch: rrf_scores[ch], reverse=True)
        tiles = []
        for ch in sorted_hashes[:top_k * 3]:
            tile = obj_map[ch]
            tiles.append(dc_replace(tile, score=rrf_scores[ch]))

        # Rerank merged results
        try:
            from isma.src.reranker import get_reranker
            reranker = get_reranker()
            if reranker.is_available():
                tiles = reranker.rerank(
                    query, tiles,
                    instruction=instruction,
                    query_type="relational",
                )
        except Exception as e:
            log.debug("Reranker unavailable for relational: %s", e)

        elapsed_ms = (time.monotonic() - t0) * 1000
        return {
            "query": query,
            "tiles": tiles[:top_k],
            "total_tokens": sum(t.token_count for t in tiles[:top_k]),
            "search_time_ms": elapsed_ms,
            "hmm_reranked": True,
            "version": "v2",
            "sub_queries": sub_queries,
            "graph_expanded": graph_expanded,
        }

    # ── Content Backfill ───────────────────────────────────────

    def _fill_content(self, content_hashes: List[str], obj_map: Dict[str, Any]):
        """Batch-fetch content from v2 for tiles with empty content (graph-expanded).

        Uses a single OR-filter query instead of N+1 individual queries.
        Batches of 50 to stay within GraphQL complexity limits.
        """
        from dataclasses import replace as dc_replace
        batch_size = 50
        filled = 0
        for i in range(0, len(content_hashes), batch_size):
            batch = content_hashes[i:i + batch_size]
            if len(batch) == 1:
                safe_ch = _escape_gql(batch[0])
                where = f'{{ path: ["content_hash"], operator: Equal, valueText: "{safe_ch}" }}'
            else:
                operands = ", ".join(
                    f'{{ path: ["content_hash"], operator: Equal, valueText: "{_escape_gql(ch)}" }}'
                    for ch in batch
                )
                where = f'{{ operator: Or, operands: [{operands}] }}'
            q = (
                f'{{ Get {{ {V2_CLASS}('
                f'where: {where}'
                f' limit: {len(batch)}'
                f') {{ content_hash content rosetta_summary loaded_at }} }} }}'
            )
            data = _graphql(q)
            results = data.get("data", {}).get("Get", {}).get(V2_CLASS, [])
            for fetched in results:
                ch = fetched.get("content_hash", "")
                if ch in obj_map:
                    tile = obj_map[ch]
                    obj_map[ch] = dc_replace(
                        tile,
                        content=fetched.get("content", "") or tile.content,
                        rosetta_summary=fetched.get("rosetta_summary", "") or tile.rosetta_summary,
                        loaded_at=fetched.get("loaded_at", "") or tile.loaded_at,
                    )
                    filled += 1
        log.debug("Backfilled content for %d/%d graph-expanded tiles", filled, len(content_hashes))

    def _search_colbert(self, query: str, top_k: int = 30) -> List[str]:
        """Search ISMA_ColBERT with multi-vector query embedding.

        Returns content_hashes ranked by MaxSim (ColBERT late interaction).
        Uses MuVera FDE for efficient approximate MaxSim in HNSW.
        """
        import json as _json

        model = _get_colbert_model()
        if model is None:
            return []

        # Encode query as multi-vector
        emb = model.encode([query], is_query=True)
        vectors = emb[0]
        if hasattr(vectors, "tolist"):
            vectors = vectors.tolist()

        # Named vector nearVector query
        gql = (
            f'{{ Get {{ {COLBERT_CLASS}('
            f'nearVector: {{ vector: {_json.dumps(vectors)}, '
            f'targetVectors: ["colbert"] }}'
            f' limit: {top_k}'
            f') {{ content_hash _additional {{ distance }} }} }} }}'
        )
        data = _graphql(gql)
        results = (
            data.get("data", {})
            .get("Get", {})
            .get(COLBERT_CLASS, [])
        )
        return [obj["content_hash"] for obj in results if obj.get("content_hash")]

    def _fetch_v1_tiles_by_hash(
        self,
        content_hashes: List[str],
        v1,
    ) -> List:
        """Fetch V1 search_512 tiles by content_hash — parallel.

        Returns one TileResult per unique content_hash found.
        Uses ThreadPoolExecutor for concurrent fetches.
        """
        if not content_hashes:
            return []
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_one(ch):
            try:
                ch_tiles = v1.get_tiles_for_content(ch, scale="search_512")
                return ch_tiles[0] if ch_tiles else None
            except Exception:
                return None

        tiles = []
        with ThreadPoolExecutor(max_workers=min(len(content_hashes), MAX_THREAD_FANOUT)) as pool:
            futures = {pool.submit(_fetch_one, ch): ch for ch in content_hashes}
            for fut in as_completed(futures):
                tile = fut.result()
                if tile is not None:
                    tiles.append(tile)
        return tiles

    # ── Passage Expansion ───────────────────────────────────────

    def expand_passages(
        self,
        content_hash: str,
        scale: str = "search_512",
    ) -> List[TileResult]:
        """Fetch all v1 tiles for a content_hash at a given scale.

        Used to drill into specific passages after document-level search.
        """
        from isma.src.retrieval import ISMARetrieval

        r = ISMARetrieval()
        return r.get_tiles_for_content(content_hash, scale=scale)

    def _get_theme_motifs(self, query: str) -> list:
        """Route a conceptual query through ISMA_Themes to get relevant motif filter.

        Queries ISMA_Themes (24 objects — sub-millisecond nearVector) instead of
        scanning ISMA_Quantum (1M objects). Returns required_motifs of the top-matching
        theme to use as a seed-set filter predicate on the main ISMA_Quantum search.

        Returns list of motif strings (e.g. ['HMM.SACRED_TRUST']) or [] on failure.
        """
        try:
            vector = _get_embedding(query)
            if not vector:
                return []
            vector_str = str(vector)
            gql = (
                "{ Get { ISMA_Themes("
                f"nearVector: {{ vector: {vector_str}, certainty: 0.6 }}"
                " limit: 1"
                ") { theme_id display_name required_motifs _additional { distance } } } }"
            )
            data = requests.post(
                f"{WEAVIATE_URL}/v1/graphql", json={"query": gql}, timeout=3
            ).json()
            themes = (data.get("data") or {}).get("Get", {}).get("ISMA_Themes", [])
            if not themes:
                return []
            top = themes[0]
            motifs = top.get("required_motifs") or []
            dist = top.get("_additional", {}).get("distance", 1.0)
            log.debug(
                "Theme routing: %s (%s) dist=%.3f motifs=%s",
                top.get("display_name"), top.get("theme_id"), dist, motifs
            )
            return motifs
        except Exception as e:
            log.debug("Theme routing failed: %s", e)
        return []

    def _get_theme_context(self, query: str) -> str:
        """DEPRECATED — use _get_theme_motifs() instead.

        Old implementation queried ISMA_Quantum (1M tiles) with nearVector + where
        post-filter to find 24 theme tiles, causing 3.8x latency spike.
        Now delegates to _get_theme_motifs() which queries ISMA_Themes (24 objects).
        Kept for backward compatibility but returns empty string (functionality moved).
        """
        return ""

    # ── Filters ─────────────────────────────────────────────────

    def _build_filter(self, **filters) -> str:
        """Build a Weaviate where filter clause from keyword arguments."""
        conditions = []

        for key, value in filters.items():
            if value is None:
                continue
            if key == "platform":
                safe_val = str(value).replace("\\", "\\\\").replace('"', '\\"')
                conditions.append(
                    f'{{ path: ["platform"], operator: Equal, valueText: "{safe_val}" }}'
                )
            elif key == "source_type":
                safe_val = str(value).replace("\\", "\\\\").replace('"', '\\"')
                conditions.append(
                    f'{{ path: ["source_type"], operator: Equal, valueText: "{safe_val}" }}'
                )
            elif key == "hmm_enriched":
                conditions.append(
                    f'{{ path: ["hmm_enriched"], operator: Equal, valueBoolean: {"true" if value else "false"} }}'
                )
            elif key == "min_hmm_phi":
                value = float(value)
                conditions.append(
                    f'{{ path: ["hmm_phi"], operator: GreaterThanEqual, valueNumber: {value} }}'
                )
            elif key == "min_hmm_trust":
                value = float(value)
                conditions.append(
                    f'{{ path: ["hmm_trust"], operator: GreaterThanEqual, valueNumber: {value} }}'
                )
            elif key == "dominant_motifs":
                motifs = [str(m).replace("\\", "\\\\").replace('"', '\\"') for m in (value or []) if m]
                if motifs:
                    values = ", ".join(f'"{m}"' for m in motifs)
                    conditions.append(
                        f'{{ path: ["dominant_motifs"], operator: ContainsAny, valueText: [{values}] }}'
                    )
            elif key == "time_after":
                safe_val = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
                conditions.append(
                    f'{{ path: ["timestamp"], operator: GreaterThanEqual, valueText: "{safe_val}" }}'
                )
            elif key == "time_before":
                safe_val = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
                conditions.append(
                    f'{{ path: ["timestamp"], operator: LessThan, valueText: "{safe_val}" }}'
                )
            else:
                raise ValueError(f"unsupported filter key: {key}")

        if not conditions:
            return ""

        if len(conditions) == 1:
            return f", where: {conditions[0]}"
        else:
            ops = ", ".join(conditions)
            return f", where: {{ operator: And, operands: [{ops}] }}"


# Module-level singleton with thread safety
_instance: Optional[ISMARetrievalV2] = None
_instance_lock = threading.Lock()


def get_retrieval_v2() -> ISMARetrievalV2:
    """Get singleton ISMARetrievalV2 instance (thread-safe)."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ISMARetrievalV2()
    return _instance
