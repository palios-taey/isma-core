#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUERY_API = os.environ.get("ISMA_QUERY_API", "http://127.0.0.1:8095").rstrip("/")
DEFAULT_WEAVIATE_URL = os.environ.get("ISMA_WEAVIATE_URL", "http://localhost:8088").rstrip("/")
DEFAULT_SAMPLE_LIMIT = 5000
DEFAULT_CLUSTER_CAP = 35
DEFAULT_TOP_K = 10
DEFAULT_WORKERS = 1
DEFAULT_QUERY_CHARS = 1200
DEFAULT_OUT_DIR = REPO_ROOT / "benchmarks" / "results"
DEFAULT_JSON = DEFAULT_OUT_DIR / "authority_probe.json"
DEFAULT_MD = DEFAULT_OUT_DIR / "authority_probe.md"

TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
WHITESPACE_RE = re.compile(r"\s+")
CORRECTION_CUES = (
    "correction",
    "corrected",
    "fix",
    "fixed",
    "replace",
    "revised",
    "update",
    "updated",
    "erratum",
    "note:",
    "actually",
)
REFINEMENT_CUES = (
    "refine",
    "refined",
    "extends",
    "extended",
    "more detail",
    "additional",
    "builds on",
    "follow-up",
    "follow up",
    "expands",
    "clarifies",
)
OPEN_CONTEST_CUES = (
    "however",
    "but",
    "instead",
    "disagree",
    "not",
    "contradict",
    "counter",
    "unless",
    "yet",
)


class ProbeError(RuntimeError):
    pass


@dataclass
class Tile:
    tile_id: str
    content_hash: str
    source_file: str
    scale: str
    tile_index: int
    content: str
    is_superseded: bool


@dataclass
class CandidatePair:
    left_tile_id: str
    right_tile_id: str
    left_content_hash: str
    right_content_hash: str
    left_source_file: str
    right_source_file: str
    left_scale: str
    right_scale: str
    left_tile_index: int
    right_tile_index: int
    left_text: str
    right_text: str
    left_score: float
    right_score: float
    similarity: float
    jaccard: float
    length_ratio: float
    relation: str
    confidence: float
    auto_supersede: bool


def graphql_post(weaviate_url: str, query: str) -> dict[str, Any]:
    resp = requests.post(
        f"{weaviate_url}/v1/graphql",
        json={"query": query},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, dict):
        raise ProbeError("GraphQL response was not an object")
    if payload.get("errors"):
        raise ProbeError(f"GraphQL errors: {payload['errors']}")
    return payload


def query_api_post(query_api: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    delay = 0.5
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            resp = requests.post(f"{query_api}{path}", json=body, timeout=120)
            if resp.status_code >= 500 or resp.status_code == 429:
                raise ProbeError(f"{path} transient HTTP {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ProbeError(f"{path} response was not an object")
            return payload
        except (requests.RequestException, ProbeError) as exc:
            last_exc = exc
            if attempt >= 4:
                break
            time.sleep(delay)
            delay *= 2
    assert last_exc is not None
    raise ProbeError(f"{path} failed after retries: {last_exc}") from last_exc


def load_sample_tiles(weaviate_url: str, limit: int) -> list[Tile]:
    query = f"""
    {{
      Get {{
        ISMA_Quantum(limit: {limit}) {{
          content_hash
          source_file
          scale
          tile_index
          content
          is_superseded
          _additional {{ id }}
        }}
      }}
    }}
    """
    data = graphql_post(weaviate_url, query)
    rows = data["data"]["Get"]["ISMA_Quantum"]
    tiles: list[Tile] = []
    for row in rows:
        addl = row.get("_additional") or {}
        tiles.append(
            Tile(
                tile_id=str(addl.get("id") or ""),
                content_hash=str(row.get("content_hash") or ""),
                source_file=str(row.get("source_file") or ""),
                scale=str(row.get("scale") or ""),
                tile_index=int(row.get("tile_index") or 0),
                content=str(row.get("content") or ""),
                is_superseded=bool(row.get("is_superseded")),
            )
        )
    return tiles


def choose_cluster(tiles: list[Tile], cap: int) -> tuple[str, list[Tile]]:
    by_source: dict[str, list[Tile]] = collections.defaultdict(list)
    for tile in tiles:
        if tile.source_file:
            by_source[tile.source_file].append(tile)
    if not by_source:
        raise ProbeError("no non-empty source_file clusters found")
    source_file, cluster = max(by_source.items(), key=lambda kv: (len(kv[1]), kv[0]))
    cluster = sorted(cluster, key=lambda t: (t.content_hash, t.scale, t.tile_id))[:cap]
    return source_file, cluster


def fetch_cluster(weaviate_url: str, source_file: str, cap: int) -> list[Tile]:
    safe_source = json.dumps(source_file)
    query = f"""
    {{
      Get {{
        ISMA_Quantum(
          where: {{ path: ["source_file"], operator: Equal, valueText: {safe_source} }}
          limit: {cap}
        ) {{
          content_hash
          source_file
          scale
          tile_index
          content
          is_superseded
          _additional {{ id }}
        }}
      }}
    }}
    """
    data = graphql_post(weaviate_url, query)
    rows = data["data"]["Get"]["ISMA_Quantum"]
    tiles: list[Tile] = []
    for row in rows:
        addl = row.get("_additional") or {}
        tiles.append(
            Tile(
                tile_id=str(addl.get("id") or ""),
                content_hash=str(row.get("content_hash") or ""),
                source_file=str(row.get("source_file") or ""),
                scale=str(row.get("scale") or ""),
                tile_index=int(row.get("tile_index") or 0),
                content=str(row.get("content") or ""),
                is_superseded=bool(row.get("is_superseded")),
            )
        )
    return sorted(tiles, key=lambda t: (t.content_hash, t.scale, t.tile_index, t.tile_id))


def normalize_text(text: str) -> str:
    text = text.lower()
    text = text.replace("\u2019", "'")
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    return WHITESPACE_RE.sub(" ", text).strip()


def token_set(text: str) -> set[str]:
    return {t for t in TOKEN_RE.findall(normalize_text(text)) if len(t) >= 3}


def cue_score(text: str, cues: tuple[str, ...]) -> int:
    lowered = normalize_text(text)
    return sum(1 for cue in cues if cue in lowered)


def pair_metrics(left: str, right: str) -> tuple[float, float, float]:
    n_left = normalize_text(left)
    n_right = normalize_text(right)
    similarity = SequenceMatcher(None, n_left, n_right).ratio()
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    inter = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens) or 1
    jaccard = inter / union
    length_ratio = min(len(n_left), len(n_right)) / max(len(n_left), len(n_right) or 1)
    return similarity, jaccard, length_ratio


def classify_pair(left: Tile, right: Tile) -> tuple[str, float, bool]:
    similarity, jaccard, length_ratio = pair_metrics(left.content, right.content)
    dup_same_identity = (
        bool(left.content_hash)
        and left.content_hash == right.content_hash
        and left.scale == right.scale
        and int(left.tile_index) == int(right.tile_index)
    )
    correction_hits = cue_score(left.content, CORRECTION_CUES) + cue_score(right.content, CORRECTION_CUES)
    refinement_hits = cue_score(left.content, REFINEMENT_CUES) + cue_score(right.content, REFINEMENT_CUES)
    contest_hits = cue_score(left.content, OPEN_CONTEST_CUES) + cue_score(right.content, OPEN_CONTEST_CUES)

    if dup_same_identity:
        relation = "duplicate"
        confidence = 0.99
    elif correction_hits and similarity >= 0.72 and jaccard >= 0.55:
        relation = "correction"
        confidence = min(0.98, 0.82 + 0.18 * similarity)
    elif contest_hits and similarity >= 0.45:
        relation = "open-contest"
        confidence = min(0.90, 0.55 + 0.30 * similarity)
    elif refinement_hits and similarity >= 0.62:
        relation = "refinement"
        confidence = min(0.88, 0.50 + 0.35 * similarity)
    elif similarity >= 0.72 and jaccard >= 0.40:
        relation = "parallel-restatement"
        confidence = min(0.84, 0.40 + 0.45 * similarity)
    elif similarity >= 0.45:
        relation = "unrelated"
        confidence = min(0.70, 0.25 + 0.30 * similarity)
    else:
        relation = "unrelated"
        confidence = max(0.10, 0.15 + 0.20 * similarity)

    auto_supersede = relation in {"duplicate", "correction"} and confidence >= 0.92
    return relation, confidence, auto_supersede


def fetch_neighbors_for_tile(
    query_api: str,
    source_file: str,
    tile: Tile,
    top_k: int,
    query_chars: int,
) -> list[dict[str, Any]]:
    payload = query_api_post(
        query_api,
        "/search",
        {
            "query": tile.content[:query_chars],
            "top_k": top_k,
            "source_file": source_file,
            "include_superseded": False,
        },
    )
    tiles = payload.get("tiles") or []
    if not isinstance(tiles, list):
        raise ProbeError("search response missing tiles list")
    out: list[dict[str, Any]] = []
    for row in tiles:
        if not isinstance(row, dict):
            continue
        neighbor_id = str(row.get("tile_id") or "")
        if not neighbor_id or neighbor_id == tile.tile_id:
            continue
        neighbor_source = str(row.get("source_file") or "")
        if neighbor_source != source_file:
            continue
        out.append(
            {
                "seed_tile": tile,
                "neighbor": Tile(
                    tile_id=neighbor_id,
                    content_hash=str(row.get("content_hash") or ""),
                    source_file=neighbor_source,
                    scale=str(row.get("scale") or ""),
                    tile_index=int(row.get("tile_index") or 0),
                    content=str(row.get("content") or ""),
                    is_superseded=bool(row.get("is_superseded")),
                ),
                "seed_score": float(row.get("score") or 0.0),
            }
        )
    return out


def collect_candidate_pairs(
    query_api: str,
    source_file: str,
    tiles: list[Tile],
    top_k: int,
    workers: int,
    query_chars: int,
) -> list[CandidatePair]:
    pair_map: dict[tuple[str, str], dict[str, Any]] = {}
    max_workers = 1
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(fetch_neighbors_for_tile, query_api, source_file, tile, top_k, query_chars)
            for tile in tiles
        ]
        for fut in as_completed(futures):
            for edge in fut.result():
                left = edge["seed_tile"]
                right = edge["neighbor"]
                pair_key = tuple(sorted((left.tile_id, right.tile_id)))
                if pair_key not in pair_map:
                    pair_map[pair_key] = {
                        "left": left,
                        "right": right,
                        "seed_scores": [],
                    }
                pair_map[pair_key]["seed_scores"].append(float(edge["seed_score"]))

    pairs: list[CandidatePair] = []
    for pair_key, entry in pair_map.items():
        left = entry["left"]
        right = entry["right"]
        relation, confidence, auto_supersede = classify_pair(left, right)
        similarity, jaccard, length_ratio = pair_metrics(left.content, right.content)
        seed_scores = entry["seed_scores"]
        left_score = max(seed_scores) if seed_scores else 0.0
        right_score = min(seed_scores) if seed_scores else 0.0
        pairs.append(
            CandidatePair(
                left_tile_id=left.tile_id,
                right_tile_id=right.tile_id,
                left_content_hash=left.content_hash,
                right_content_hash=right.content_hash,
                left_source_file=left.source_file,
                right_source_file=right.source_file,
                left_scale=left.scale,
                right_scale=right.scale,
                left_tile_index=left.tile_index,
                right_tile_index=right.tile_index,
                left_text=left.content,
                right_text=right.content,
                left_score=left_score,
                right_score=right_score,
                similarity=similarity,
                jaccard=jaccard,
                length_ratio=length_ratio,
                relation=relation,
                confidence=confidence,
                auto_supersede=auto_supersede,
            )
        )

    pairs.sort(key=lambda p: (-p.confidence, -p.similarity, p.left_tile_id, p.right_tile_id))
    return pairs


def summarize(pairs: list[CandidatePair]) -> dict[str, Any]:
    relation_counts = collections.Counter(pair.relation for pair in pairs)
    auto_pairs = [pair for pair in pairs if pair.auto_supersede]
    review_pairs = [pair for pair in pairs if not pair.auto_supersede]
    return {
        "pair_count": len(pairs),
        "auto_supersede_count": len(auto_pairs),
        "review_queue_count": len(review_pairs),
        "relation_counts": dict(sorted(relation_counts.items())),
        "auto_supersede_rate": round(len(auto_pairs) / len(pairs), 4) if pairs else 0.0,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Authority Probe Report",
        "",
        f"- query_api: `{report['query_api']}`",
        f"- weaviate_url: `{report['weaviate_url']}`",
        f"- selected_source_file: `{report['source_file']}`",
        f"- cluster_size: `{report['cluster_size']}`",
        f"- sample_limit: `{report['sample_limit']}`",
        f"- cluster_cap: `{report['cluster_cap']}`",
        f"- top_k: `{report['top_k']}`",
        f"- query_chars: `{report['query_chars']}`",
        f"- pair_count: `{report['pair_count']}`",
        f"- auto_supersede_count: `{report['auto_supersede_count']}`",
        f"- review_queue_count: `{report['review_queue_count']}`",
        f"- auto_supersede_rate: `{report['auto_supersede_rate']}`",
        "",
        "## Relation Counts",
    ]
    for relation, count in report["relation_counts"].items():
        lines.append(f"- {relation}: {count}")
    lines.extend(["", "## Top Pairs"])
    for pair in report["pairs"][:10]:
        lines.extend(
            [
                f"### {pair['left_tile_id']} ↔ {pair['right_tile_id']}",
                f"- relation: {pair['relation']}",
                f"- confidence: {pair['confidence']}",
                f"- auto_supersede: {str(pair['auto_supersede']).lower()}",
                f"- similarity: {pair['similarity']}",
                f"- jaccard: {pair['jaccard']}",
                f"- length_ratio: {pair['length_ratio']}",
                f"- left_scale/right_scale: {pair['left_scale']} / {pair['right_scale']}",
                f"- left_tile_index/right_tile_index: {pair['left_tile_index']} / {pair['right_tile_index']}",
                f"- left_score/right_score: {pair['left_score']} / {pair['right_score']}",
                f"- left_text: {pair['left_text'][:500].replace(chr(10), ' ')}",
                f"- right_text: {pair['right_text'][:500].replace(chr(10), ' ')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_report(
    *,
    query_api: str,
    weaviate_url: str,
    sample_limit: int,
    cluster_cap: int,
    top_k: int,
    query_chars: int,
    source_file: str,
    cluster_size: int,
    pairs: list[CandidatePair],
) -> dict[str, Any]:
    summary = summarize(pairs)
    return {
        "query_api": query_api,
        "weaviate_url": weaviate_url,
        "sample_limit": sample_limit,
        "cluster_cap": cluster_cap,
        "top_k": top_k,
        "query_chars": query_chars,
        "source_file": source_file,
        "cluster_size": cluster_size,
        **summary,
        "pairs": [asdict(pair) for pair in pairs],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only authority-probe prototype for same-idea / supersede gating.")
    parser.add_argument("--query-api", default=DEFAULT_QUERY_API)
    parser.add_argument("--weaviate-url", default=DEFAULT_WEAVIATE_URL)
    parser.add_argument("--sample-limit", type=int, default=DEFAULT_SAMPLE_LIMIT)
    parser.add_argument("--cluster-cap", type=int, default=DEFAULT_CLUSTER_CAP)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--query-chars", type=int, default=DEFAULT_QUERY_CHARS)
    parser.add_argument("--out-json", default=str(DEFAULT_JSON))
    parser.add_argument("--out-md", default=str(DEFAULT_MD))
    parser.add_argument("--source-file", default="")
    args = parser.parse_args()

    sample_tiles = load_sample_tiles(args.weaviate_url, args.sample_limit)
    if args.source_file:
        source_file = args.source_file
        cluster_tiles = fetch_cluster(args.weaviate_url, source_file, args.cluster_cap)
    else:
        source_file, cluster_tiles = choose_cluster(sample_tiles, args.cluster_cap)
        if len(cluster_tiles) < min(args.cluster_cap, 5):
            cluster_tiles = fetch_cluster(args.weaviate_url, source_file, args.cluster_cap)

    if not cluster_tiles:
        raise ProbeError("selected cluster was empty")

    pairs = collect_candidate_pairs(
        args.query_api,
        source_file,
        cluster_tiles,
        args.top_k,
        args.workers,
        args.query_chars,
    )

    report = build_report(
        query_api=args.query_api,
        weaviate_url=args.weaviate_url,
        sample_limit=args.sample_limit,
        cluster_cap=args.cluster_cap,
        top_k=args.top_k,
        query_chars=args.query_chars,
        source_file=source_file,
        cluster_size=len(cluster_tiles),
        pairs=pairs,
    )

    out_json = Path(args.out_json).resolve()
    out_md = Path(args.out_md).resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    out_md.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("source_file", "cluster_size", "pair_count", "auto_supersede_count", "review_queue_count", "auto_supersede_rate")}, indent=2, sort_keys=True))
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
