#!/usr/bin/env python3
"""Verify the `authority` query filter end-to-end using an EPHEMERAL fixture.

Why a fixture instead of labelling real tiles: choosing what is authoritative
(and the label vocabulary for it) is a governance decision, not an engineering
one. This script proves the FILTER MECHANISM works without writing any lasting
authority label into the corpus — it creates throwaway tiles, asserts the
filter both ways, and tears them down.

Falsifiable in BOTH directions, which is the point:
  * SELECT  — a query WITH the filter returns ONLY fixture tiles.
  * EXCLUDE — the same query WITHOUT the filter returns real corpus tiles,
              proving the filter actually narrowed rather than silently
              matching everything.
  * NEGATIVE — a filter value that labels nothing returns ZERO results,
              proving a non-matching filter is applied rather than ignored.

That last assertion is the one that matters most: a filter silently dropped by
the request model returns unfiltered results with HTTP 200, which looks exactly
like success. That is how this filter first appeared "broken" (0/12) when in
fact the param was being ignored by a request model that did not declare it.

Usage:
    python3 isma/scripts/verify_authority_filter.py [--api http://localhost:8095]

Exits non-zero on any failed assertion. Always tears the fixture down, including
on failure, and verifies zero fixture tiles remain.
"""
import argparse
import json
import sys
import urllib.request
import uuid

FIXTURE_LABEL = "__authority_filter_fixture__"   # deliberately throwaway, never a real scheme value
FIXTURE_MARK = "zqxwrbl"                          # nonsense token so the fixture cannot collide with corpus text
NEGATIVE_LABEL = "__label_that_matches_nothing__"


def _post(url, payload, timeout=45):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _gql(weaviate, query):
    return _post(f"{weaviate}/v1/graphql", {"query": query})


def create_fixture(weaviate, n=3):
    """Insert n ephemeral tiles carrying the fixture authority label."""
    ids = []
    for i in range(n):
        oid = str(uuid.uuid4())
        body = {
            "class": "ISMA_Quantum",
            "id": oid,
            "properties": {
                "content": f"{FIXTURE_MARK} authority filter fixture tile {i}. "
                           f"Ephemeral verification object; safe to delete.",
                "source_file": f"/__fixture__/{FIXTURE_MARK}_{i}.md",
                "authority": FIXTURE_LABEL,
                "scale": "search_512",
                "is_superseded": False,
            },
        }
        req = urllib.request.Request(
            f"{weaviate}/v1/objects", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
        ids.append(oid)
    return ids


def teardown_fixture(weaviate):
    """Delete every fixture tile. Returns how many remain (must be 0)."""
    q = ('{ Get { ISMA_Quantum(limit:200, where:{path:["authority"],'
         'operator:Equal,valueText:"%s"}) { _additional { id } } } }' % FIXTURE_LABEL)
    tiles = (_gql(weaviate, q)["data"]["Get"]["ISMA_Quantum"] or [])
    for t in tiles:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{weaviate}/v1/objects/ISMA_Quantum/{t['_additional']['id']}",
                method="DELETE"), timeout=30)
        except Exception as e:                      # noqa: BLE001 - report, never swallow
            print(f"  teardown: failed to delete {t['_additional']['id']}: {e}")
    remaining = (_gql(weaviate, q)["data"]["Get"]["ISMA_Quantum"] or [])
    return len(remaining)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8095")
    ap.add_argument("--weaviate", default="http://localhost:8088")
    args = ap.parse_args()

    failures = []
    created = 0
    try:
        created = len(create_fixture(args.weaviate))
        print(f"  fixture: created {created} ephemeral tiles labelled {FIXTURE_LABEL}")

        # 1. SELECT — with the filter, only fixture tiles come back.
        got = _post(f"{args.api}/search/bm25",
                    {"query": FIXTURE_MARK, "top_k": 10,
                     "authority": FIXTURE_LABEL}).get("tiles", [])
        foreign = [t for t in got if FIXTURE_LABEL != (t.get("authority") or "")]
        if not got:
            failures.append("SELECT: filter returned nothing; fixture tiles should match")
        elif foreign:
            failures.append(f"SELECT: filter returned {len(foreign)} non-fixture tiles")
        print(f"  SELECT  : {len(got)} tiles, all fixture-labelled = {not foreign}")

        # 2. EXCLUDE — the filter must actually narrow. An unfiltered query for a
        #    common corpus term returns real tiles; the same query filtered to the
        #    fixture label must NOT return those real tiles.
        unfiltered = _post(f"{args.api}/search/bm25",
                           {"query": "ISMA ingestion", "top_k": 5}).get("tiles", [])
        filtered = _post(f"{args.api}/search/bm25",
                         {"query": "ISMA ingestion", "top_k": 5,
                          "authority": FIXTURE_LABEL}).get("tiles", [])
        leaked = [t for t in filtered if FIXTURE_LABEL != (t.get("authority") or "")]
        if not unfiltered:
            failures.append("EXCLUDE: unfiltered control returned nothing; cannot judge narrowing")
        if leaked:
            failures.append(f"EXCLUDE: {len(leaked)} unlabelled corpus tiles leaked through the filter")
        print(f"  EXCLUDE : unfiltered={len(unfiltered)} filtered={len(filtered)} leaked={len(leaked)}")

        # 3. NEGATIVE — a label matching nothing must return zero, proving the
        #    filter is applied rather than silently dropped by the request model.
        none_ = _post(f"{args.api}/search/bm25",
                      {"query": FIXTURE_MARK, "top_k": 10,
                       "authority": NEGATIVE_LABEL}).get("tiles", [])
        if none_:
            failures.append(
                f"NEGATIVE: non-matching label returned {len(none_)} tiles — "
                "the filter is being IGNORED, not applied")
        print(f"  NEGATIVE: non-matching label returned {len(none_)} tiles (must be 0)")
    finally:
        remaining = teardown_fixture(args.weaviate)
        print(f"  teardown: {remaining} fixture tiles remain (must be 0)")
        if remaining:
            failures.append(f"TEARDOWN: {remaining} fixture tiles left behind")

    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — authority filter selects, excludes, and is genuinely applied; "
          "zero persistent authority labels written to the corpus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
