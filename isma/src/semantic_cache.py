"""
ISMA Semantic Cache — Query-level result caching with semantic similarity.

Caches search results in Redis keyed by query embedding similarity.
A new query that is cosine-similar > 0.95 to a cached query returns the
cached result directly (< 50ms vs ~2s uncached).

Cache invalidation:
  - On HMM enrichment: `invalidate_for_tile(content_hash)` removes cached
    results that contain the enriched tile
  - TTL-based: exact queries 1h, semantic queries 15min, adaptive 30min
  - Manual: `clear()` wipes all cache entries

Keys:
  isma:cache:query:{hash} — cached result JSON
  isma:cache:vectors — hash of known query vectors (for similarity check)
  isma:cache:stats — hit/miss counters

Usage:
    from isma.src.semantic_cache import SemanticCache

    cache = SemanticCache()

    # Check cache before search
    cached = cache.get(query, query_type="adaptive")
    if cached:
        return cached

    # After search, store result
    cache.put(query, result, query_type="adaptive")

    # Invalidate after enrichment
    cache.invalidate_for_tile(content_hash)
"""

import hashlib
import json
import logging
import math
import time
from typing import Any, Dict, List, Optional

import redis
from isma.config import REDIS_HOST as CONFIG_REDIS_HOST, REDIS_PORT as CONFIG_REDIS_PORT

log = logging.getLogger(__name__)

REDIS_HOST = CONFIG_REDIS_HOST
REDIS_PORT = CONFIG_REDIS_PORT

PREFIX = "isma:cache:"

# TTL by query type (seconds)
TTLS = {
    "exact": 3600,       # 1 hour — factual queries are stable
    "temporal": 900,     # 15 min — time-sensitive
    "conceptual": 1800,  # 30 min
    "relational": 1800,  # 30 min
    "memory": 1800,      # 30 min — conversational recall
    "humor": 1800,       # 30 min — conversational content
    "motif": 1800,       # 30 min
    "adaptive": 1800,    # 30 min (default for adaptive_search)
    "default": 1800,
}

# Cosine similarity threshold for cache hit
SIMILARITY_THRESHOLD = 0.95

# Maximum number of cached query vectors to check
MAX_VECTOR_CHECK = 200


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _query_hash(query: str, query_type: str = "default", **filters) -> str:
    """Deterministic hash for exact cache matching.

    Includes query text, query type, and all filter kwargs to prevent
    cross-filter contamination (e.g., platform=gemini vs platform=grok).
    """
    parts = [query.strip().lower(), query_type]
    for k in sorted(filters.keys()):
        if filters[k] is not None:
            parts.append(f"{k}={filters[k]}")
    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:24]


class SemanticCache:
    """Query-level semantic cache backed by Redis."""

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)

    def get(
        self,
        query: str,
        query_type: str = "default",
        embedding: Optional[List[float]] = None,
        **filters,
    ) -> Optional[Dict[str, Any]]:
        """Check cache for a query. Returns cached result or None.

        First checks exact match (fast), then semantic similarity if
        embedding is provided. Filters (platform, source_type, etc.) are
        included in the cache key to prevent cross-filter contamination.
        """
        # 1. Exact match (hash-based, includes filters)
        qhash = _query_hash(query, query_type, **filters)
        cached = self.r.get(f"{PREFIX}query:{qhash}")
        if cached:
            self._incr_stat("hits_exact")
            try:
                return json.loads(cached)
            except (json.JSONDecodeError, TypeError):
                self.r.delete(f"{PREFIX}query:{qhash}")

        # 2. Semantic similarity (if embedding provided)
        if embedding:
            similar = self._find_similar(embedding)
            if similar:
                self._incr_stat("hits_semantic")
                return similar

        self._incr_stat("misses")
        return None

    def put(
        self,
        query: str,
        result: Dict[str, Any],
        query_type: str = "default",
        embedding: Optional[List[float]] = None,
        **filters,
    ):
        """Store a search result in cache.

        Args:
            query: The search query text
            result: The search result dict (must be JSON-serializable)
            query_type: For TTL selection
            embedding: Query embedding for semantic matching
            **filters: Additional filters (platform, source_type, etc.)
        """
        qhash = _query_hash(query, query_type, **filters)
        ttl = TTLS.get(query_type, TTLS["default"])

        # Prepare cache entry
        entry = {
            "query": query,
            "query_type": query_type,
            "cached_at": time.time(),
            "result": self._make_serializable(result),
        }

        try:
            self.r.set(
                f"{PREFIX}query:{qhash}",
                json.dumps(entry),
                ex=ttl,
            )
        except Exception as e:
            log.warning("Cache put failed: %s", e)
            return

        # Build reverse index: tile → [qhash, ...] for O(1) invalidation.
        # Replaces the O(N) scan_iter in invalidate_for_tile().
        try:
            tiles = result.get("tiles", []) if isinstance(result, dict) else []
            for tile in tiles:
                ch = tile.get("content_hash") if isinstance(tile, dict) else getattr(tile, "content_hash", None)
                if ch:
                    self.r.sadd(f"{PREFIX}tile:{ch}", qhash)
                    self.r.expire(f"{PREFIX}tile:{ch}", ttl)
        except Exception as e:
            log.debug("Reverse tile index put failed: %s", e)

        # Store embedding for semantic matching
        if embedding:
            try:
                self.r.set(
                    f"{PREFIX}vec:{qhash}",
                    json.dumps(embedding),
                    ex=ttl,
                )
                # Track which hashes have vectors
                self.r.sadd(f"{PREFIX}vec_index", qhash)
            except Exception as e:
                log.debug("Vector cache put failed: %s", e)

        self._incr_stat("puts")

    def invalidate_for_tile(self, content_hash: str):
        """Invalidate cached results that contain a specific tile.

        Called after HMM enrichment updates a tile.

        Uses reverse index (isma:cache:tile:{hash}) for O(1) lookup instead
        of O(N) scan_iter over all cached queries.
        """
        count = 0
        try:
            qhashes = self.r.smembers(f"{PREFIX}tile:{content_hash}")
            if qhashes:
                pipe = self.r.pipeline()
                for qhash in qhashes:
                    pipe.delete(f"{PREFIX}query:{qhash}")
                    pipe.delete(f"{PREFIX}vec:{qhash}")
                    pipe.srem(f"{PREFIX}vec_index", qhash)
                pipe.delete(f"{PREFIX}tile:{content_hash}")
                pipe.execute()
                count = len(qhashes)
        except Exception as e:
            log.warning("Cache invalidation failed for tile %s: %s", content_hash[:16], e)

        if count:
            log.info("Cache invalidated %d entries for tile %s", count, content_hash[:16])
        self._incr_stat("invalidations", count)

    def clear(self):
        """Clear all cache entries."""
        count = 0
        for key in self.r.scan_iter(f"{PREFIX}*", count=1000):
            self.r.delete(key)
            count += 1
        log.info("Cache cleared: %d keys", count)

    def stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        raw = self.r.hgetall(f"{PREFIX}stats")
        stats = {k: int(v) for k, v in raw.items()}

        # Count cached entries
        query_keys = list(self.r.scan_iter(f"{PREFIX}query:*", count=1000))
        vec_count = self.r.scard(f"{PREFIX}vec_index") or 0

        stats["cached_queries"] = len(query_keys)
        stats["cached_vectors"] = vec_count
        return stats

    def _find_similar(self, embedding: List[float]) -> Optional[Dict]:
        """Find a cached result with similar query embedding."""
        vec_hashes = self.r.srandmember(f"{PREFIX}vec_index", MAX_VECTOR_CHECK)
        if not vec_hashes:
            return None

        best_sim = 0.0
        best_hash = None

        for qhash in vec_hashes:
            raw = self.r.get(f"{PREFIX}vec:{qhash}")
            if not raw:
                self.r.srem(f"{PREFIX}vec_index", qhash)
                continue
            try:
                cached_vec = json.loads(raw)
                sim = _cosine_similarity(embedding, cached_vec)
                if sim > best_sim:
                    best_sim = sim
                    best_hash = qhash
            except (json.JSONDecodeError, TypeError):
                continue

        if best_sim >= SIMILARITY_THRESHOLD and best_hash:
            cached = self.r.get(f"{PREFIX}query:{best_hash}")
            if cached:
                try:
                    return json.loads(cached)
                except (json.JSONDecodeError, TypeError):
                    pass

        return None

    def _make_serializable(self, obj: Any) -> Any:
        """Make a result dict JSON-serializable."""
        if isinstance(obj, dict):
            return {k: self._make_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._make_serializable(item) for item in obj]
        if hasattr(obj, '__dataclass_fields__'):
            from dataclasses import asdict
            return asdict(obj)
        return obj

    def _incr_stat(self, stat: str, count: int = 1):
        """Increment a cache statistic."""
        try:
            self.r.hincrby(f"{PREFIX}stats", stat, count)
        except Exception:
            pass
