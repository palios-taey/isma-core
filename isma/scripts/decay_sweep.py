#!/usr/bin/env python3
"""Memory decay sweep — DRY-RUN / report-only.

Walks the vector store and FLAGS entries the memory-governance policy
(see MEMORY_GOVERNANCE.md) considers evictable candidates: tiles whose
`is_superseded` boolean flag is true (a newer version replaced them).
These are already excluded from retrieval by default; the sweep surfaces
them as archival/eviction candidates.

Why a boolean flag, not `superseded_by != ""`: `superseded_by` is a
word-tokenized text field, so Weaviate rejects an empty-string filter
(`valueText: ""` → "only stopwords provided"). The `is_superseded`
boolean filters reliably; supersede-on-write sets it alongside
`superseded_by`/`invalidated_at`.

This tool NEVER deletes. Destructive eviction is intentionally a SEPARATE,
explicitly-flagged, backed-up operation — not something a routine sweep does.
(careful-reversible-writes discipline.)

It FAILS LOUD: if the store is unreachable or returns errors it raises rather
than silently reporting zero — a silent zero would falsely read as "nothing to
evict" when the truth is "could not check".

Usage:
    python3 -m isma.scripts.decay_sweep            # report evictable candidates
    python3 -m isma.scripts.decay_sweep --limit 25 # larger sample
    python3 -m isma.scripts.decay_sweep --json     # machine-readable

Requires WEAVIATE_URL (see isma/config.py / .env.example). Read-only.
"""

import argparse
import json
import sys

import requests

from isma.config import WEAVIATE_URL, WEAVIATE_CLASS


def _graphql(query: str) -> dict:
    """POST a GraphQL query; raise loudly on any failure (never swallow)."""
    url = f"{WEAVIATE_URL}/v1/graphql"
    try:
        resp = requests.post(url, json={"query": query}, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"decay_sweep: cannot reach Weaviate at {url}: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(
            f"decay_sweep: Weaviate {url} returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"decay_sweep: GraphQL errors from {url}: {data['errors']}")
    return data["data"]


def _count(where: str = "") -> int:
    if where:
        q = f"{{ Aggregate {{ {WEAVIATE_CLASS}(where: {where}) {{ meta {{ count }} }} }} }}"
    else:
        q = f"{{ Aggregate {{ {WEAVIATE_CLASS} {{ meta {{ count }} }} }} }}"
    agg = _graphql(q)["Aggregate"][WEAVIATE_CLASS]
    return int(agg[0]["meta"]["count"]) if agg else 0


def _sample(where: str, limit: int) -> list:
    q = (
        f"{{ Get {{ {WEAVIATE_CLASS}(where: {where}, limit: {limit}) {{ "
        f"content_hash source_file scale superseded_by invalidated_at valid_from "
        f"_additional {{ id }} }} }} }}"
    )
    return _graphql(q)["Get"][WEAVIATE_CLASS] or []


# Policy predicate — boolean flag (reliable), not empty-string text.
SUPERSEDED_WHERE = '{ path: ["is_superseded"], operator: Equal, valueBoolean: true }'


def sweep(limit: int) -> dict:
    total = _count()
    superseded_n = _count(SUPERSEDED_WHERE)
    return {
        "store": WEAVIATE_URL,
        "class": WEAVIATE_CLASS,
        "total_tiles": total,
        "superseded": {
            "count": superseded_n,
            "sample": _sample(SUPERSEDED_WHERE, limit) if superseded_n else [],
        },
        "note": (
            "DRY-RUN: report only, nothing deleted. is_superseded=true tiles are "
            "already excluded from retrieval; they are archival/eviction candidates. "
            "Scope-based decay (session/project) needs a scope field not yet in the "
            "schema and is intentionally NOT approximated here. Eviction is a "
            "separate, explicitly-flagged, backed-up operation."
        ),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Memory decay sweep (dry-run / report-only).")
    ap.add_argument("--limit", type=int, default=10, help="sample size (default 10)")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args(argv)

    report = sweep(args.limit)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"Decay sweep (DRY-RUN) — {report['class']} @ {report['store']}")
    print(f"  total tiles:                    {report['total_tiles']}")
    print(f"  superseded (evict candidates):  {report['superseded']['count']}")
    sample = report["superseded"]["sample"]
    if sample:
        print(f"  --- sample (up to {args.limit}) ---")
        for row in sample:
            print(
                f"    {row.get('source_file','?')} "
                f"[scale={row.get('scale','?')}] "
                f"superseded_by={row.get('superseded_by','') or '-'} "
                f"invalidated_at={row.get('invalidated_at','') or '-'}"
            )
    print(f"  NOTE: {report['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
