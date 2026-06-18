"""
ISMA Agentic Retry — Thin retry loop for hard queries.

When the first retrieval attempt returns low-quality results (top score < 0.3),
this module:
  1. Reclassifies the query with looser thresholds
  2. Expands filters (e.g., removes platform constraint)
  3. Retries once with the adjusted strategy
  4. Returns the best available result with diagnostic metadata

Max 2 attempts total (initial + 1 retry). Never blocks on retries.

Usage:
    from isma.src.agentic_retry import retrieval_with_retry

    result = retrieval_with_retry(query, top_k=10)
    # result includes 'retry_info' with diagnostics
"""

import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Minimum top score to consider results acceptable
# RRF scores are typically 0.01-0.20 — below 0.01 means very poor matching
MIN_ACCEPTABLE_SCORE = 0.01

# Maximum retry attempts
MAX_RETRIES = 1


def retrieval_with_retry(
    query: str,
    top_k: int = 10,
    **filters,
) -> Dict[str, Any]:
    """Execute adaptive search with one retry on low-quality results.

    Returns the best result (first attempt or retry), plus retry_info
    diagnostic metadata.
    """
    from isma.src.retrieval_v2 import get_retrieval_v2

    r = get_retrieval_v2()
    t0 = time.monotonic()

    # First attempt: normal adaptive search
    result = r.adaptive_search(query, top_k=top_k, **filters)

    top_score = _get_top_score(result)
    first_strategy = result.get("strategy", "unknown")

    retry_info = {
        "attempts": 1,
        "first_strategy": first_strategy,
        "first_top_score": top_score,
        "retried": False,
    }

    # Check if results are acceptable
    if top_score >= MIN_ACCEPTABLE_SCORE:
        result["retry_info"] = retry_info
        return result

    log.info("Low quality (top_score=%.3f) for '%s' — retrying with expanded strategy",
             top_score, query[:60])

    # Retry: expand strategy
    retry_filters = dict(filters)
    retry_result = _retry_with_expansion(r, query, top_k, first_strategy, retry_filters)

    retry_top_score = _get_top_score(retry_result) if retry_result else 0.0

    retry_info["attempts"] = 2
    retry_info["retried"] = True
    retry_info["retry_strategy"] = retry_result.get("strategy", "unknown") if retry_result else None
    retry_info["retry_top_score"] = retry_top_score

    # Return whichever result is better
    if retry_result and retry_top_score > top_score:
        log.info("Retry improved: %.3f -> %.3f", top_score, retry_top_score)
        retry_result["retry_info"] = retry_info
        retry_result["search_time_ms"] = (time.monotonic() - t0) * 1000
        return retry_result
    else:
        log.info("Retry did not improve (%.3f vs %.3f), keeping original", retry_top_score, top_score)
        result["retry_info"] = retry_info
        result["search_time_ms"] = (time.monotonic() - t0) * 1000
        return result


def _retry_with_expansion(
    retrieval,
    query: str,
    top_k: int,
    first_strategy: str,
    filters: dict,
) -> Optional[Dict[str, Any]]:
    """Retry search with expanded strategy and loosened filters.

    Expansion strategies:
    1. Remove platform filter (search all platforms)
    2. If strategy was specific (temporal/motif), fall back to conceptual
    3. Increase fetch multiplier by searching for more candidates
    """
    # Remove platform constraint for broader search
    expanded_filters = {k: v for k, v in filters.items() if k != "platform"}

    # Choose alternative strategy
    alt_strategy = _choose_alternative_strategy(first_strategy)

    try:
        # Use hybrid_search with the alternative strategy
        result = retrieval.hybrid_search(
            query,
            top_k=top_k,
            rerank=True,
            query_type=alt_strategy,
            instruction=f"Find the most relevant content for: {query}",
            **expanded_filters,
        )
        result["strategy"] = f"{alt_strategy}_retry"
        return result
    except Exception as e:
        log.warning("Retry failed: %s", e)
        return None


def _choose_alternative_strategy(first_strategy: str) -> str:
    """Pick an alternative strategy for retry.

    Mapping:
    - exact -> conceptual (broaden the search semantically)
    - temporal -> default (drop time constraints)
    - motif -> conceptual (use semantic instead of motif keywords)
    - relational -> conceptual (simpler single-query approach)
    - conceptual -> default (most general)
    - default -> conceptual
    """
    alternatives = {
        "exact": "conceptual",
        "temporal": "default",
        "motif": "conceptual",
        "relational": "conceptual",
        "conceptual": "default",
        "default": "conceptual",
    }
    return alternatives.get(first_strategy, "default")


def _get_top_score(result: Dict[str, Any]) -> float:
    """Get the highest score from result tiles."""
    tiles = result.get("tiles", [])
    if not tiles:
        return 0.0

    scores = []
    for t in tiles:
        if hasattr(t, "score"):
            scores.append(t.score)
        elif isinstance(t, dict):
            scores.append(t.get("score", 0.0))

    return max(scores) if scores else 0.0
