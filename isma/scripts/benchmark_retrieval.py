#!/usr/bin/env python3
"""
ISMA Retrieval Benchmark Runner

Runs 100 queries across 5 categories against the current ISMA retrieval pipeline,
measuring Recall@k, MRR, Precision@k, latency, and dedup ratio.

Usage:
    python3 benchmark_retrieval.py                    # Full run, save to the configured benchmark output dir
    python3 benchmark_retrieval.py --output /tmp/bench.json
    python3 benchmark_retrieval.py --category exact   # Single category
    python3 benchmark_retrieval.py --dry-run           # Parse queries only, no search
"""

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

# Add parent paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from isma.src.retrieval import ISMARetrieval, SearchResult, TileResult
from isma.src.retrieval_v2 import ISMARetrievalV2
from isma.config import ISMA_BENCHMARK_OUTPUT_DIR as CONFIG_BENCHMARK_OUTPUT_DIR


# =============================================================================
# METRICS
# =============================================================================

def _tile_text(tile: "TileResult") -> str:
    """Build searchable text for a tile: content + rosetta_summary + dominant_motifs.

    dominant_motifs (e.g. ['HMM.SACRED_TRUST']) are included so that expected_content
    terms like 'SACRED_TRUST' match even when they don't appear verbatim in the prose.
    Term check: 'sacred_trust' is a substring of 'hmm.sacred_trust' → correct match.
    """
    motif_text = " ".join(tile.dominant_motifs or [])
    return ((tile.content or "") + " " + (tile.rosetta_summary or "") + " " + motif_text).lower()


def recall_at_k(retrieved_hashes: List[str], expected_content: List[str],
                tiles: List[TileResult], k: int) -> float:
    """Fraction of expected content terms found in top-k tiles.

    Each tile is scored independently (max 2000 chars content+rosetta, full motifs)
    to prevent cross-tile boundary matches from inflating recall.
    """
    if not expected_content:
        return -1.0  # No ground truth available

    # Build per-tile text (content+rosetta capped at 2000 chars, motifs uncapped)
    tile_texts = [_tile_text(t)[:2000] for t in tiles[:k]]
    # A term is "found" if ANY individual tile contains it
    found = sum(
        1 for term in expected_content
        if any(term.lower() in text for text in tile_texts)
    )
    return found / len(expected_content)


def mrr(expected_content: List[str], tiles: List[TileResult]) -> float:
    """Mean Reciprocal Rank: 1/rank of first tile containing ANY expected term."""
    if not expected_content:
        return -1.0

    for i, tile in enumerate(tiles):
        text = _tile_text(tile)[:2000]
        if any(term.lower() in text for term in expected_content):
            return 1.0 / (i + 1)
    return 0.0


def precision_at_k(expected_content: List[str], tiles: List[TileResult],
                   k: int) -> float:
    """Fraction of top-k tiles that contain at least one expected term."""
    if not expected_content:
        return -1.0

    relevant = 0
    for tile in tiles[:k]:
        text = _tile_text(tile)[:2000]
        if any(term.lower() in text for term in expected_content):
            relevant += 1
    return relevant / min(k, len(tiles)) if tiles else 0.0


def dedup_ratio(tiles: List[TileResult], k: int) -> float:
    """Ratio of unique content_hashes to total tiles in top-k.
    1.0 = perfect (no duplicates), <1.0 = fragmentation."""
    top_k = tiles[:k]
    if not top_k:
        return 1.0
    unique = len(set(t.content_hash for t in top_k if t.content_hash))
    return unique / len(top_k)


def motif_precision_at_k(tiles: List[TileResult], expected_motifs: List[str], k: int) -> float:
    """Fraction of top-k tiles that contain at least one expected motif.

    Uses dominant_motifs field — measures whether motif routing returned
    motif-relevant tiles rather than random semantic matches.
    """
    if not expected_motifs or not tiles:
        return -1.0
    expected_set = set(expected_motifs)
    hits = sum(
        1 for t in tiles[:k]
        if expected_set & set(t.dominant_motifs or [])
    )
    return hits / min(k, len(tiles))


def gold_recall_at_k(tiles: List[TileResult], gold_hash: str, k: int) -> float:
    """Binary hit/miss for gold_content_hash in top-k results."""
    if not gold_hash:
        return -1.0
    top_k = tiles[:k]
    for tile in top_k:
        if tile.content_hash == gold_hash:
            return 1.0
    return 0.0


def gold_mrr(tiles: List[TileResult], gold_hash: str) -> float:
    """Mean Reciprocal Rank based on gold_content_hash."""
    if not gold_hash:
        return -1.0
    for i, tile in enumerate(tiles):
        if tile.content_hash == gold_hash:
            return 1.0 / (i + 1)
    return 0.0


# =============================================================================
# QUERY RUNNER
# =============================================================================

def run_query(retrieval: ISMARetrieval, query_def: Dict[str, Any],
              top_k: int = 10, v2: Optional["ISMARetrievalV2"] = None) -> Dict[str, Any]:
    """Run a single benchmark query and compute metrics."""
    query = query_def["query"]
    category = query_def["category"]
    expected_content = query_def.get("expected_content", [])
    gold_hash = query_def.get("gold_content_hash")

    # Build filter kwargs
    filters = {}
    if query_def.get("platform_hint"):
        filters["platform"] = query_def["platform_hint"]

    # Choose retrieval method based on category
    t0 = time.monotonic()

    if category in ("motif", "relational"):
        # Motif and relational queries use ISMARetrievalV2.adaptive_search()
        # which routes through specialized paths:
        #   - motif → _search_motif() (motif-filtered hybrid + reranker)
        #   - relational → _search_relational_cascade() (Dream Cycle architecture)
        expected_motifs = query_def.get("expected_motifs", [])
        _v2 = v2 or ISMARetrievalV2()
        result = _v2.adaptive_search(query, top_k=top_k, **filters)
        tiles = result.get("tiles", [])
        latency_ms = result.get("search_time_ms", (time.monotonic() - t0) * 1000)

    else:
        # All other categories: use hybrid_retrieve_hmm
        hmm_result = retrieval.hybrid_retrieve_hmm(
            query, top_k=top_k,
            hmm_rerank_enabled=True,
            expand_graph=False,
            graph_depth=1,
            **filters,
        )
        tiles = hmm_result.get("tiles", [])
        latency_ms = hmm_result.get("search_time_ms", (time.monotonic() - t0) * 1000)

    # Compute metrics
    if category == "motif":
        # Motif: use motif_precision@10 as R@10 (fraction of results containing expected motif)
        expected_motifs = query_def.get("expected_motifs", [])
        mp10 = motif_precision_at_k(tiles, expected_motifs, 10)
        mp5 = motif_precision_at_k(tiles, expected_motifs, 5)
        result = {
            "query_id": query_def["id"],
            "category": category,
            "difficulty": query_def.get("difficulty", "medium"),
            "query": query,
            "latency_ms": round(latency_ms, 2),
            "num_results": len(tiles),
            "recall_5": round(mp5, 4),   # motif precision@5 reported as recall_5
            "recall_10": round(mp10, 4), # motif precision@10 reported as recall_10
            "gold_recall_10": round(gold_recall_at_k(tiles, gold_hash, 10), 4) if gold_hash else -1.0,
            "mrr": round(mp10, 4),
            "gold_mrr": round(gold_mrr(tiles, gold_hash), 4) if gold_hash else -1.0,
            "precision_5": round(mp5, 4),
            "precision_10": round(mp10, 4),
            "dedup_5": round(dedup_ratio(tiles, 5), 4),
            "dedup_10": round(dedup_ratio(tiles, 10), 4),
            "top_3_hashes": [(t.content_hash or "")[:12] for t in tiles[:3]],
            "top_3_platforms": [(t.platform or "") for t in tiles[:3]],
            "enriched_in_top10": sum(1 for t in tiles[:10] if t.hmm_enriched),
            "expected_motifs": expected_motifs,
        }
    else:
        result = {
            "query_id": query_def["id"],
            "category": category,
            "difficulty": query_def.get("difficulty", "medium"),
            "query": query,
            "latency_ms": round(latency_ms, 2),
            "num_results": len(tiles),
            "recall_5": round(recall_at_k([], expected_content, tiles, 5), 4),
            "recall_10": round(recall_at_k([], expected_content, tiles, 10), 4),
            "gold_recall_10": round(gold_recall_at_k(tiles, gold_hash, 10), 4) if gold_hash else -1.0,
            "mrr": round(mrr(expected_content, tiles), 4),
            "gold_mrr": round(gold_mrr(tiles, gold_hash), 4) if gold_hash else -1.0,
            "precision_5": round(precision_at_k(expected_content, tiles, 5), 4),
            "precision_10": round(precision_at_k(expected_content, tiles, 10), 4),
            "dedup_5": round(dedup_ratio(tiles, 5), 4),
            "dedup_10": round(dedup_ratio(tiles, 10), 4),
            "top_3_hashes": [(t.content_hash or "")[:12] for t in tiles[:3]],
            "top_3_platforms": [(t.platform or "") for t in tiles[:3]],
            "enriched_in_top10": sum(1 for t in tiles[:10] if t.hmm_enriched),
        }

    return result


def run_query_v2(retrieval_v2, query_def: Dict[str, Any],
                 top_k: int = 10) -> Dict[str, Any]:
    """Run a single benchmark query using ISMARetrievalV2.adaptive_search()."""
    query = query_def["query"]
    category = query_def["category"]
    expected_content = query_def.get("expected_content", [])
    gold_hash = query_def.get("gold_content_hash")

    filters = {}
    if query_def.get("platform_hint"):
        filters["platform"] = query_def["platform_hint"]

    t0 = time.monotonic()
    result = retrieval_v2.adaptive_search(query, top_k=top_k, **filters)
    latency_ms = (time.monotonic() - t0) * 1000

    tiles = result.get("tiles", [])
    strategy = result.get("strategy", "unknown")
    diagnostics = result.get("diagnostics", {})

    # Compute metrics
    if category == "motif":
        expected_motifs = query_def.get("expected_motifs", [])
        mp10 = motif_precision_at_k(tiles, expected_motifs, 10)
        mp5 = motif_precision_at_k(tiles, expected_motifs, 5)
        metrics = {
            "query_id": query_def["id"],
            "category": category,
            "difficulty": query_def.get("difficulty", "medium"),
            "query": query,
            "strategy": strategy,
            "latency_ms": round(latency_ms, 2),
            "num_results": len(tiles),
            "recall_5": round(mp5, 4),
            "recall_10": round(mp10, 4),
            "gold_recall_10": round(gold_recall_at_k(tiles, gold_hash, 10), 4) if gold_hash else -1.0,
            "mrr": round(mp10, 4),
            "gold_mrr": round(gold_mrr(tiles, gold_hash), 4) if gold_hash else -1.0,
            "precision_5": round(mp5, 4),
            "precision_10": round(mp10, 4),
            "dedup_5": round(dedup_ratio(tiles, 5), 4),
            "dedup_10": round(dedup_ratio(tiles, 10), 4),
            "top_3_hashes": [(t.content_hash or "")[:12] for t in tiles[:3]],
            "top_3_platforms": [(t.platform or "") for t in tiles[:3]],
            "enriched_in_top10": sum(1 for t in tiles[:10] if t.hmm_enriched),
            "expected_motifs": expected_motifs,
        }
    else:
        metrics = {
            "query_id": query_def["id"],
            "category": category,
            "difficulty": query_def.get("difficulty", "medium"),
            "query": query,
            "strategy": strategy,
            "latency_ms": round(latency_ms, 2),
            "num_results": len(tiles),
            "recall_5": round(recall_at_k([], expected_content, tiles, 5), 4),
            "recall_10": round(recall_at_k([], expected_content, tiles, 10), 4),
            "gold_recall_10": round(gold_recall_at_k(tiles, gold_hash, 10), 4) if gold_hash else -1.0,
            "mrr": round(mrr(expected_content, tiles), 4),
            "gold_mrr": round(gold_mrr(tiles, gold_hash), 4) if gold_hash else -1.0,
            "precision_5": round(precision_at_k(expected_content, tiles, 5), 4),
            "precision_10": round(precision_at_k(expected_content, tiles, 10), 4),
            "dedup_5": round(dedup_ratio(tiles, 5), 4),
            "dedup_10": round(dedup_ratio(tiles, 10), 4),
            "top_3_hashes": [(t.content_hash or "")[:12] for t in tiles[:3]],
            "top_3_platforms": [(t.platform or "") for t in tiles[:3]],
            "enriched_in_top10": sum(1 for t in tiles[:10] if t.hmm_enriched),
        }

    # 6C.0 Instrumentation: include diagnostics for oracle recall analysis
    if diagnostics:
        metrics["candidate_count"] = diagnostics.get("candidate_count", 0)
        metrics["v2_overlay_count"] = diagnostics.get("v2_overlay_count", 0)
        metrics["classifier_confidence"] = diagnostics.get("classifier_confidence", 0)

        # Oracle recall: check if expected terms appear in candidate pool at various K
        candidate_texts = diagnostics.get("candidate_texts", [])
        if expected_content and candidate_texts:
            oracle = {}
            for k in [10, 15, 18, 20, 25, 30]:
                pool = [t.lower() for t in candidate_texts[:k]]
                found = sum(
                    1 for term in expected_content
                    if any(term.lower() in text for text in pool)
                )
                oracle[f"oracle_R@{k}"] = round(found / len(expected_content), 4)
            metrics["oracle_recall"] = oracle

        metrics["candidate_pool_utilization"] = (
            len(tiles) / max(len(candidate_texts), len(diagnostics.get("candidate_hashes", [])), 1)
        )

        # 6C.0: Per-step timing breakdown
        timing = diagnostics.get("timing_ms")
        if timing:
            metrics["timing_ms"] = timing

    return metrics


# =============================================================================
# AGGREGATION
# =============================================================================

def aggregate_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate per-query results into category and overall summaries."""
    by_category = defaultdict(list)
    for r in results:
        by_category[r["category"]].append(r)

    summary = {"overall": {}, "by_category": {}, "by_difficulty": {}}

    # Overall
    valid = [r for r in results if r["recall_10"] >= 0]
    summary["overall"] = _agg_metrics(valid)

    # By category
    for cat, cat_results in by_category.items():
        valid_cat = [r for r in cat_results if r["recall_10"] >= 0]
        summary["by_category"][cat] = _agg_metrics(valid_cat)

    # By difficulty
    by_diff = defaultdict(list)
    for r in results:
        by_diff[r["difficulty"]].append(r)
    for diff, diff_results in by_diff.items():
        valid_diff = [r for r in diff_results if r["recall_10"] >= 0]
        summary["by_difficulty"][diff] = _agg_metrics(valid_diff)

    return summary


def _agg_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute aggregate metrics from a list of query results."""
    if not results:
        return {"count": 0}

    latencies = sorted(r["latency_ms"] for r in results)

    def _mean(key):
        vals = [r[key] for r in results if r.get(key, -1) >= 0]
        return round(sum(vals) / len(vals), 4) if vals else -1.0

    def _percentile(arr, p):
        if not arr:
            return 0
        idx = int(len(arr) * p / 100)
        return round(arr[min(idx, len(arr) - 1)], 2)

    agg = {
        "count": len(results),
        "recall_5_mean": _mean("recall_5"),
        "recall_10_mean": _mean("recall_10"),
        "gold_recall_10_mean": _mean("gold_recall_10"),
        "mrr_mean": _mean("mrr"),
        "gold_mrr_mean": _mean("gold_mrr"),
        "precision_5_mean": _mean("precision_5"),
        "precision_10_mean": _mean("precision_10"),
        "dedup_5_mean": _mean("dedup_5"),
        "dedup_10_mean": _mean("dedup_10"),
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
        "latency_p99_ms": _percentile(latencies, 99),
        "enriched_in_top10_mean": round(
            sum(r["enriched_in_top10"] for r in results) / len(results), 2
        ),
    }

    # Oracle recall means (if available)
    oracle_results = [r for r in results if "oracle_recall" in r]
    if oracle_results:
        for k_label in ["oracle_R@10", "oracle_R@15", "oracle_R@18", "oracle_R@20", "oracle_R@25", "oracle_R@30"]:
            vals = [r["oracle_recall"][k_label] for r in oracle_results if k_label in r["oracle_recall"]]
            if vals:
                agg[k_label + "_mean"] = round(sum(vals) / len(vals), 4)

    return agg


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="ISMA Retrieval Benchmark")
    parser.add_argument("--output", "-o", default=None,
                        help="Output file path (default: <configured benchmark output dir>/benchmark_<timestamp>.json)")
    parser.add_argument("--queries", "-q",
                        default=os.path.join(os.path.dirname(__file__), "benchmark_queries_v2.json"),
                        help="Path to benchmark queries JSON")
    parser.add_argument("--category", "-c", default=None,
                        help="Run only this category (exact, temporal, conceptual, relational, motif)")
    parser.add_argument("--top-k", "-k", type=int, default=10,
                        help="Top-k results per query (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse queries only, don't run searches")
    parser.add_argument("--label", "-l", default=None,
                        help="Label for this benchmark run (e.g., 'baseline', 'phase1')")
    parser.add_argument("--v2", action="store_true",
                        help="Use ISMARetrievalV2.adaptive_search() instead of v1")
    parser.add_argument("--colbert", action="store_true",
                        help="Add ColBERT MaxSim signal via RRF fusion with V1 (requires ISMA_ColBERT_Pilot)")
    parser.add_argument("--colbert-weight", type=float, default=0.3,
                        help="RRF weight for ColBERT signal (default: 0.3, V1 gets 0.7)")
    args = parser.parse_args()

    # Load queries
    with open(args.queries) as f:
        query_data = json.load(f)

    queries = query_data["queries"]
    if args.category:
        queries = [q for q in queries if q["category"] == args.category]

    print(f"Loaded {len(queries)} queries", end="")
    if args.category:
        print(f" (category: {args.category})", end="")
    print()

    if args.dry_run:
        for q in queries:
            print(f"  {q['id']}: {q['query'][:60]}...")
        print(f"\nDry run complete. {len(queries)} queries parsed.")
        return

    # Initialize retrieval (always init V2 for motif queries)
    if args.v2:
        print("Initializing ISMARetrievalV2 (adaptive)...")
        retrieval_v2 = ISMARetrievalV2()
    else:
        print("Initializing ISMARetrieval...")
        retrieval_v2 = ISMARetrievalV2()  # needed for motif category regardless
    retrieval = ISMARetrieval()

    # Prewarm reranker to avoid 5-9s cold-start penalty on first query
    try:
        from isma.src.reranker import get_reranker
        reranker = get_reranker()
        if reranker.is_available():
            print("Reranker prewarmed")
    except Exception:
        pass

    colbert_retrieval = None
    if args.colbert:
        print(f"Initializing ColBERT retrieval (weight={args.colbert_weight}, V1 weight={1.0 - args.colbert_weight})...")
        from isma.scripts.colbert_retrieval import ColBERTRetrieval, rrf_fusion
        colbert_retrieval = ColBERTRetrieval()
        print("ColBERT ready.")

    # Run queries
    results = []
    total = len(queries)
    t_start = time.monotonic()

    for i, q in enumerate(queries):
        print(f"  [{i+1}/{total}] {q['id']}: {q['query'][:50]}...", end="", flush=True)
        try:
            if args.v2:
                result = run_query_v2(retrieval_v2, q, top_k=args.top_k)
            else:
                result = run_query(retrieval, q, top_k=args.top_k, v2=retrieval_v2)

            # ColBERT RRF fusion: re-rank using ColBERT MaxSim + V1 combined
            if colbert_retrieval and not args.v2:
                try:
                    v1_hashes = [t.content_hash for t in result.get("tiles", [])]
                    cb_hashes = colbert_retrieval.search_content_hashes(q["query"], top_k=args.top_k * 2)
                    fused_hashes = rrf_fusion(
                        [v1_hashes, cb_hashes],
                        weights=[1.0 - args.colbert_weight, args.colbert_weight],
                        top_n=args.top_k,
                    )
                    # Re-score with fused ranking
                    expected = q.get("expected_content", [])
                    result["recall_10_colbert"] = recall_at_k(fused_hashes, expected, k=10)
                    result["recall_5_colbert"] = recall_at_k(fused_hashes, expected, k=5)
                    result["colbert_fused_hashes"] = fused_hashes
                except Exception as ce:
                    print(f" ColBERT fusion failed for {q['id']}: {ce}")

            results.append(result)
            status = f" R@10={result['recall_10']:.2f} MRR={result['mrr']:.2f} {result['latency_ms']:.0f}ms"
            print(status)
        except Exception as e:
            print(f" ERROR: {e}")
            results.append({
                "query_id": q["id"],
                "category": q["category"],
                "difficulty": q.get("difficulty", "medium"),
                "query": q["query"],
                "error": str(e),
                "latency_ms": 0,
                "num_results": 0,
                "recall_5": -1, "recall_10": -1, "mrr": -1,
                "precision_5": -1, "precision_10": -1,
                "dedup_5": -1, "dedup_10": -1,
                "top_3_hashes": [], "top_3_platforms": [],
                "enriched_in_top10": 0,
            })

    elapsed = time.monotonic() - t_start

    # Aggregate
    summary = aggregate_results(results)

    # Build output
    # ColBERT comparison summary (if run)
    colbert_summary = None
    if colbert_retrieval:
        cb_results = [r for r in results if "recall_10_colbert" in r]
        if cb_results:
            cb_r10 = sum(r["recall_10_colbert"] for r in cb_results) / len(cb_results)
            v1_r10 = sum(r.get("recall_10", 0) for r in cb_results) / len(cb_results)
            colbert_summary = {
                "queries_with_colbert": len(cb_results),
                "v1_recall_10": round(v1_r10, 4),
                "v1_colbert_rrf_recall_10": round(cb_r10, 4),
                "delta": round(cb_r10 - v1_r10, 4),
                "colbert_weight": args.colbert_weight,
                "gate_passed": cb_r10 - v1_r10 >= 0.05,  # +5% gate
            }
            print(f"\nColBERT RRF result: V1={v1_r10:.4f} → V1+ColBERT={cb_r10:.4f} "
                  f"(delta={cb_r10 - v1_r10:+.4f}, gate={'PASS' if colbert_summary['gate_passed'] else 'FAIL'})")

    output = {
        "benchmark_version": query_data.get("version", "1.0.0"),
        "label": args.label or "baseline",
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "top_k": args.top_k,
            "category_filter": args.category,
            "total_queries": len(queries),
            "total_time_seconds": round(elapsed, 2),
            "colbert_enabled": bool(colbert_retrieval),
            "colbert_weight": args.colbert_weight if colbert_retrieval else None,
        },
        "summary": summary,
        "colbert_comparison": colbert_summary,
        "details": results,
    }

    # Save
    if args.output:
        output_path = args.output
    else:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(CONFIG_BENCHMARK_OUTPUT_DIR, f"benchmark_{ts}.json")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Benchmark complete: {len(results)} queries in {elapsed:.1f}s")
    print(f"Results saved to: {output_path}")
    print(f"\nOverall:")
    ovr = summary["overall"]
    print(f"  Recall@10 (Soft): {ovr.get('recall_10_mean', -1):.3f}")
    print(f"  Recall@10 (Gold): {ovr.get('gold_recall_10_mean', -1):.3f}")
    print(f"  MRR:        {ovr.get('mrr_mean', -1):.3f}")
    print(f"  Precision@10: {ovr.get('precision_10_mean', -1):.3f}")
    print(f"  Dedup@10:   {ovr.get('dedup_10_mean', -1):.3f}")
    print(f"  Latency p50: {ovr.get('latency_p50_ms', 0):.0f}ms")
    print(f"  Latency p95: {ovr.get('latency_p95_ms', 0):.0f}ms")

    print(f"\nBy Category:")
    for cat in ["exact", "temporal", "conceptual", "relational", "motif"]:
        cat_data = summary["by_category"].get(cat, {})
        if cat_data.get("count", 0) > 0:
            print(f"  {cat:12s}: R@10={cat_data['recall_10_mean']:.3f}  "
                  f"MRR={cat_data['mrr_mean']:.3f}  "
                  f"Dedup={cat_data['dedup_10_mean']:.3f}  "
                  f"p95={cat_data['latency_p95_ms']:.0f}ms")

    # Also save a symlink to latest
    latest_path = os.path.join(CONFIG_BENCHMARK_OUTPUT_DIR, "benchmark_latest.json")
    try:
        if os.path.islink(latest_path):
            os.unlink(latest_path)
        os.symlink(output_path, latest_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
