#!/usr/bin/env python3
"""Measure HMM-adjacent prose retrieval routes.

This is the follow-up harness for the 2026-08-04 `/search/hmm` measurement.
It keeps the same six phrasings, two scale labels, and top_k=30 where a route
accepts a natural-language query. Motif-ID routes are measured separately
because their public contract does not accept a natural-language query.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TOP_K = 30
SCALES = ("full_4096", "search_512")
PHRASINGS = [
    "CPT training sequence length decision 2560 4096 8192 which did we choose",
    "packing scheme EOS separators document boundary attention masking decision",
    "continued pretraining config sequence length packing decision record",
    "27B dense CPT run GB10 unified memory sequence length chosen",
    "why we picked the training window size for the codebase CPT",
    "document boundary masking versus fixed block concatenation what we decided",
]


def post(api: str, path: str, payload: dict[str, Any], timeout: int) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except Exception:
            data = {"error": str(exc)}
        return exc.code, data


def hash_for(tile: dict[str, Any]) -> str:
    value = tile.get("content_hash") or tile.get("tile_hash") or tile.get("tile_id")
    return str(value or "")


def tiles_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(data.get("tiles"), list):
        return [t for t in data["tiles"] if isinstance(t, dict)]
    if isinstance(data.get("tiles_with_amplitude"), list):
        return [t for t in data["tiles_with_amplitude"] if isinstance(t, dict)]
    return []


def source_file(tile: dict[str, Any]) -> str:
    return str(tile.get("source_file") or "")


def is_md_prose(tile: dict[str, Any]) -> bool:
    return source_file(tile).endswith(".md")


def summarize_tile_set(tiles: list[dict[str, Any]]) -> dict[str, Any]:
    hashes = [hash_for(tile) for tile in tiles if hash_for(tile)]
    unique = {hash_for(tile): tile for tile in tiles if hash_for(tile)}
    scores = [float(tile.get("score") or 0.0) for tile in tiles if tile.get("score") is not None]
    return {
        "returned": len(tiles),
        "distinct": len(unique),
        "hashes": sorted(unique),
        "md_prose": sum(1 for tile in unique.values() if is_md_prose(tile)),
        "source_file_present": sum(1 for tile in unique.values() if "source_file" in tile),
        "content_present": sum(1 for tile in unique.values() if "content" in tile),
        "hmm_enriched_present": sum(1 for tile in unique.values() if "hmm_enriched" in tile),
        "hmm_enriched_true": sum(1 for tile in unique.values() if tile.get("hmm_enriched") is True),
        "scales": sorted({str(tile.get("scale") or "") for tile in unique.values()}),
        "score_max": max(scores) if scores else None,
        "score_median": statistics.median(scores) if scores else None,
    }


def sweep_http_query_route(
    api: str,
    path: str,
    *,
    top_k: int,
    timeout: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra = extra or {}
    calls: list[dict[str, Any]] = []
    by_query: list[dict[str, Any]] = []
    route_union: dict[str, dict[str, Any]] = {}
    for idx, phrasing in enumerate(PHRASINGS, 1):
        per_scale: dict[str, list[dict[str, Any]]] = {}
        for scale in SCALES:
            payload = {"query": phrasing, "top_k": top_k, "scale": scale, **extra}
            status, data = post(api, path, payload, timeout)
            tiles = tiles_from(data)
            per_scale[scale] = tiles
            for tile in tiles:
                h = hash_for(tile)
                if h:
                    route_union.setdefault(h, tile)
            calls.append({
                "query_index": idx,
                "scale": scale,
                "status": status,
                "count": len(tiles),
                "summary": summarize_tile_set(tiles),
                "response_keys": sorted(data.keys()),
            })
        full_hashes = {hash_for(t) for t in per_scale["full_4096"] if hash_for(t)}
        search_hashes = {hash_for(t) for t in per_scale["search_512"] if hash_for(t)}
        union_tiles = {
            hash_for(tile): tile
            for tile in per_scale["full_4096"] + per_scale["search_512"]
            if hash_for(tile)
        }
        by_query.append({
            "query_index": idx,
            "query": phrasing,
            "full_4096": summarize_tile_set(per_scale["full_4096"]),
            "search_512": summarize_tile_set(per_scale["search_512"]),
            "cross_scale_overlap": len(full_hashes & search_hashes),
            "cross_scale_distinct": len(full_hashes | search_hashes),
            "cross_scale_identical": full_hashes == search_hashes,
            "cross_scale_md_prose": sum(1 for tile in union_tiles.values() if is_md_prose(tile)),
        })
    return {
        "path": path,
        "extra": extra,
        "calls": calls,
        "by_query": by_query,
        "union": summarize_tile_set(list(route_union.values())),
        "cross_scale_overlap_sum": sum(q["cross_scale_overlap"] for q in by_query),
        "cross_scale_distinct_sum": sum(q["cross_scale_distinct"] for q in by_query),
        "cross_scale_md_prose_sum": sum(q["cross_scale_md_prose"] for q in by_query),
        "identical_scale_queries": sum(1 for q in by_query if q["cross_scale_identical"]),
    }


def compare_matching_calls(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    comparisons = []
    for call_a, call_b in zip(a["calls"], b["calls"]):
        hashes_a = set(call_a["summary"]["hashes"])
        hashes_b = set(call_b["summary"]["hashes"])
        comparisons.append({
            "query_index": call_a["query_index"],
            "scale": call_a["scale"],
            "overlap": len(hashes_a & hashes_b),
            "distinct": len(hashes_a | hashes_b),
            "identical": hashes_a == hashes_b,
            "only_a": len(hashes_a - hashes_b),
            "only_b": len(hashes_b - hashes_a),
        })
    return {
        "identical_calls": sum(1 for item in comparisons if item["identical"]),
        "total_calls": len(comparisons),
        "overlap_sum": sum(item["overlap"] for item in comparisons),
        "distinct_sum": sum(item["distinct"] for item in comparisons),
        "calls": comparisons,
    }


def import_motif_assigner():
    from isma.src.hmm.motifs import assign_motifs

    return assign_motifs


def assigned_motifs() -> list[dict[str, Any]]:
    assign_motifs = import_motif_assigner()
    rows = []
    for idx, phrasing in enumerate(PHRASINGS, 1):
        motifs = assign_motifs(phrasing)
        rows.append({
            "query_index": idx,
            "query": phrasing,
            "motifs": [
                {
                    "motif_id": item.motif_id,
                    "amp": item.amp,
                    "confidence": item.confidence,
                }
                for item in motifs
            ],
        })
    return rows


def sweep_http_motif_route(
    api: str,
    *,
    top_k: int,
    timeout: int,
    motif_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    route_union: dict[str, dict[str, Any]] = {}
    for row in motif_rows:
        per_query_union: dict[str, dict[str, Any]] = {}
        motif_calls = []
        for motif in row["motifs"]:
            motif_id = motif["motif_id"]
            status, data = post(
                api,
                "/search/motif",
                {"motif_id": motif_id, "top_k": top_k, "scale": "full_4096"},
                timeout,
            )
            tiles = tiles_from(data)
            for tile in tiles:
                h = hash_for(tile)
                if h:
                    per_query_union.setdefault(h, tile)
                    route_union.setdefault(h, tile)
            motif_calls.append({
                "motif_id": motif_id,
                "status": status,
                "response_keys": sorted(data.keys()),
                "total_candidates": data.get("total_candidates"),
                "summary": summarize_tile_set(tiles),
            })
        rows.append({
            "query_index": row["query_index"],
            "query": row["query"],
            "motifs": row["motifs"],
            "motif_calls": motif_calls,
            "union": summarize_tile_set(list(per_query_union.values())),
        })
    return {
        "path": "/search/motif",
        "contract": "motif_id required; natural-language query and scale are not request fields",
        "by_query": rows,
        "union": summarize_tile_set(list(route_union.values())),
        "queries_with_no_motif": sum(1 for row in motif_rows if not row["motifs"]),
    }


def probe_http_motif_query_payload(api: str, *, timeout: int) -> dict[str, Any]:
    status, data = post(
        api,
        "/search/motif",
        {"query": PHRASINGS[0], "top_k": TOP_K, "scale": "full_4096"},
        timeout,
    )
    return {
        "status": status,
        "response": data,
    }


def sweep_mcp_motif_search(*, top_k: int, motif_rows: list[dict[str, Any]]) -> dict[str, Any]:
    from isma.src.mcp_server import handle_isma_motif_search

    rows = []
    route_union: dict[str, dict[str, Any]] = {}
    for row in motif_rows:
        per_query_union: dict[str, dict[str, Any]] = {}
        motif_calls = []
        for motif in row["motifs"]:
            motif_id = motif["motif_id"]
            data = handle_isma_motif_search({
                "motif_id": motif_id,
                "min_amplitude": 0.5,
                "limit": top_k,
            })
            tiles = tiles_from(data)
            for tile in tiles:
                h = hash_for(tile)
                if h:
                    per_query_union.setdefault(h, tile)
                    route_union.setdefault(h, tile)
            motif_calls.append({
                "motif_id": motif_id,
                "response_keys": sorted(data.keys()),
                "total_candidates": data.get("total_candidates"),
                "summary": summarize_tile_set(tiles),
            })
        rows.append({
            "query_index": row["query_index"],
            "query": row["query"],
            "motifs": row["motifs"],
            "motif_calls": motif_calls,
            "union": summarize_tile_set(list(per_query_union.values())),
        })
    return {
        "tool": "isma_motif_search",
        "contract": "motif_id required; natural-language query and scale are not tool input fields",
        "by_query": rows,
        "union": summarize_tile_set(list(route_union.values())),
        "queries_with_no_motif": sum(1 for row in motif_rows if not row["motifs"]),
    }


def sweep_mcp_isma_search(*, top_k: int, enriched_only: bool) -> dict[str, Any]:
    from isma.src.mcp_server import handle_isma_search

    calls = []
    by_query = []
    route_union: dict[str, dict[str, Any]] = {}
    for idx, phrasing in enumerate(PHRASINGS, 1):
        per_scale: dict[str, list[dict[str, Any]]] = {}
        for scale in SCALES:
            data = handle_isma_search({
                "query": phrasing,
                "top_k": top_k,
                "scale": scale,
                "enriched_only": enriched_only,
            })
            tiles = tiles_from(data)
            per_scale[scale] = tiles
            for tile in tiles:
                h = hash_for(tile)
                if h:
                    route_union.setdefault(h, tile)
            calls.append({
                "query_index": idx,
                "scale": scale,
                "count": len(tiles),
                "summary": summarize_tile_set(tiles),
                "response_keys": sorted(data.keys()),
                "note": data.get("note"),
            })
        full_hashes = {hash_for(t) for t in per_scale["full_4096"] if hash_for(t)}
        search_hashes = {hash_for(t) for t in per_scale["search_512"] if hash_for(t)}
        union_tiles = {
            hash_for(tile): tile
            for tile in per_scale["full_4096"] + per_scale["search_512"]
            if hash_for(tile)
        }
        by_query.append({
            "query_index": idx,
            "query": phrasing,
            "full_4096": summarize_tile_set(per_scale["full_4096"]),
            "search_512": summarize_tile_set(per_scale["search_512"]),
            "cross_scale_overlap": len(full_hashes & search_hashes),
            "cross_scale_distinct": len(full_hashes | search_hashes),
            "cross_scale_identical": full_hashes == search_hashes,
            "cross_scale_md_prose": sum(1 for tile in union_tiles.values() if is_md_prose(tile)),
        })
    return {
        "tool": "isma_search",
        "enriched_only": enriched_only,
        "calls": calls,
        "by_query": by_query,
        "union": summarize_tile_set(list(route_union.values())),
        "cross_scale_overlap_sum": sum(q["cross_scale_overlap"] for q in by_query),
        "cross_scale_distinct_sum": sum(q["cross_scale_distinct"] for q in by_query),
        "cross_scale_md_prose_sum": sum(q["cross_scale_md_prose"] for q in by_query),
        "identical_scale_queries": sum(1 for q in by_query if q["cross_scale_identical"]),
    }


def print_summary(results: dict[str, Any]) -> None:
    def line(name: str, item: dict[str, Any]) -> None:
        union = item["union"]
        print(
            f"{name:<32} distinct={union['distinct']:>3} md_prose={union['md_prose']:>3} "
            f"content_fields={union['content_present']:>3} source_fields={union['source_file_present']:>3} "
            f"hmm_field={union['hmm_enriched_present']:>3}"
        )

    print("HMM-adjacent route prose coverage measurement")
    print(f"API={results['api']} top_k={results['top_k']} phrasings={len(PHRASINGS)} scales={SCALES}")
    print()
    for key in [
        "http_search",
        "http_v2_search_hmm",
        "http_search_enriched_only_extra",
        "http_search_hmm_enriched_true",
        "http_search_motif",
        "mcp_isma_motif_search",
        "mcp_isma_search",
        "mcp_isma_search_enriched_only",
    ]:
        if key in results:
            line(key, results[key])
    print()
    print("set comparisons")
    for key, comparison in results["comparisons"].items():
        print(
            f"{key:<46} identical_calls={comparison['identical_calls']}/{comparison['total_calls']} "
            f"overlap_sum={comparison['overlap_sum']} distinct_sum={comparison['distinct_sum']}"
        )
    print()
    print("motif assignment")
    for row in results["motif_rows"]:
        motifs = ", ".join(item["motif_id"] for item in row["motifs"]) or "(none)"
        print(f"  q{row['query_index']}: {motifs}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=os.environ.get("ISMA_QUERY_API", "http://localhost:8095"))
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    started = time.time()
    results: dict[str, Any] = {
        "api": args.api,
        "top_k": args.top_k,
        "scales": list(SCALES),
        "phrasings": PHRASINGS,
        "started_at": started,
        "observed_code_contract": {
            "v2_search_hmm": "V2SearchRequest has no scale field; scale in payload is ignored by the request model.",
            "search_motif": "MotifSearchRequest requires motif_id and has no query or scale field.",
            "mcp_isma_motif_search": "MCP tool schema requires motif_id and has no query or scale field.",
            "http_search_enriched_only": "SearchRequest has hmm_enriched, not enriched_only; enriched_only in payload is ignored by the request model.",
            "mcp_isma_search_enriched_only": "MCP isma_search exposes enriched_only and post-filters returned TileResult objects by t.hmm_enriched.",
        },
    }

    http_search = sweep_http_query_route(args.api, "/search", top_k=args.top_k, timeout=args.timeout)
    http_v2_hmm = sweep_http_query_route(args.api, "/v2/search/hmm", top_k=args.top_k, timeout=args.timeout)
    http_enriched_extra = sweep_http_query_route(
        args.api,
        "/search",
        top_k=args.top_k,
        timeout=args.timeout,
        extra={"enriched_only": True},
    )
    http_hmm_enriched_true = sweep_http_query_route(
        args.api,
        "/search",
        top_k=args.top_k,
        timeout=args.timeout,
        extra={"hmm_enriched": True},
    )
    motif_rows = assigned_motifs()
    http_motif = sweep_http_motif_route(
        args.api,
        top_k=args.top_k,
        timeout=args.timeout,
        motif_rows=motif_rows,
    )
    http_motif_query_payload = probe_http_motif_query_payload(args.api, timeout=args.timeout)
    mcp_motif = sweep_mcp_motif_search(top_k=args.top_k, motif_rows=motif_rows)
    mcp_search = sweep_mcp_isma_search(top_k=args.top_k, enriched_only=False)
    mcp_search_enriched = sweep_mcp_isma_search(top_k=args.top_k, enriched_only=True)

    results.update({
        "http_search": http_search,
        "http_v2_search_hmm": http_v2_hmm,
        "http_search_enriched_only_extra": http_enriched_extra,
        "http_search_hmm_enriched_true": http_hmm_enriched_true,
        "motif_rows": motif_rows,
        "http_search_motif": http_motif,
        "http_search_motif_query_payload": http_motif_query_payload,
        "mcp_isma_motif_search": mcp_motif,
        "mcp_isma_search": mcp_search,
        "mcp_isma_search_enriched_only": mcp_search_enriched,
        "comparisons": {
            "http_search_vs_v2_search_hmm": compare_matching_calls(http_search, http_v2_hmm),
            "http_search_vs_http_search_enriched_only_extra": compare_matching_calls(http_search, http_enriched_extra),
            "http_search_vs_http_search_hmm_enriched_true": compare_matching_calls(http_search, http_hmm_enriched_true),
            "mcp_isma_search_vs_mcp_isma_search_enriched_only": compare_matching_calls(mcp_search, mcp_search_enriched),
        },
        "elapsed_seconds": round(time.time() - started, 1),
    })

    print_summary(results)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {path}")
    else:
        json.dump(results, sys.stdout, indent=2, sort_keys=True)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
