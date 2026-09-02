#!/usr/bin/env python3
"""Production observation suite for the ISMA memory path.

Every check here observes the RUNNING system. None of them trusts a name, a
version number, a health flag, or this repository's own documentation — each one
executes the capability and reports what actually came back.

Design rules, each learned from a real defect on this system:

  * A GREEN FLAG IS NOT A CAPABILITY. `:8089/health` has returned 200 while every
    real embed 500'd, causing a two-day silent-stale outage. So the embedding
    check performs a real embed and inspects the vector.
  * A RANKING SURFACE CANNOT VERIFY PRESENCE. Rank depends on the other 1.6M
    tiles, not on whether your document is there, so presence is checked with a
    FILTER and never with an unfiltered query.
  * A CHECK THAT CANNOT FAIL PROVES NOTHING. Where a check has an obvious
    always-passes shape, it is paired with a negative control that must come back
    empty (see the authority and deprecation checks).
  * WRITES CLEAN UP AFTER THEMSELVES. The one round-trip that writes deletes its
    own objects and asserts zero remain, in a `finally`.

Usage:
    PYTHONPATH=$ISMA_HOME python3 isma/scripts/validate_production.py
    [--api http://localhost:8095] [--weaviate http://localhost:8088]
    [--embed http://localhost:8089]

Exit 0 only if every check passes. Intended to be re-run independently by a
reviewer; it takes no arguments that change what it asserts.
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

RESULTS = []


def check(name, ok, observation):
    RESULTS.append((name, bool(ok), observation))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {observation}")
    return bool(ok)


def post(url, payload, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def post_status(url, payload, timeout=60):
    """Return (status, body) without raising — for fail-loud checks.

    Catches URLError/OSError as well as HTTPError. An unreachable service must
    produce a FAIL line and a summary, never a traceback: a suite that crashes
    skips its own reporting, and a crash is easy to misread as a passing run that
    happened to be noisy.
    """
    try:
        return post(url, payload, timeout)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}
    except Exception as e:                                   # noqa: BLE001
        return 0, {"detail": {"unreachable": str(e)[:120]}}


def gql(weaviate, query, timeout=60):
    return post(f"{weaviate}/v1/graphql", {"query": query}, timeout)[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8095")
    ap.add_argument("--weaviate", default="http://localhost:8088")
    ap.add_argument("--embed", default="http://localhost:8089")
    a = ap.parse_args()

    print("ISMA PRODUCTION OBSERVATION SUITE")
    print(f"  api={a.api}  weaviate={a.weaviate}  embed={a.embed}\n")

    # ---- 1. Corpus is live and non-trivial -------------------------------
    try:
        d = gql(a.weaviate, "{ Aggregate { ISMA_Quantum { meta { count } } } }")
        n = d["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
        check("corpus present", n > 1_000_000, f"ISMA_Quantum = {n:,} tiles")
    except Exception as e:
        check("corpus present", False, f"query failed: {e}")

    # ---- 2. Canonical semantic retrieval ---------------------------------
    try:
        _, d = post(f"{a.api}/search", {"query": "what do we know about sacred trust", "top_k": 5})
        t = d.get("tiles", [])
        top = float(t[0]["score"]) if t else 0.0
        check("canonical /search returns ranked prose", len(t) > 0 and top > 0.2,
              f"{len(t)} tiles, top score {top:.3f}, source {(t[0].get('source_file') or '?').split('/')[-1][:44]}")
    except Exception as e:
        check("canonical /search returns ranked prose", False, f"{e}")

    # ---- 3. Exact-term retrieval -----------------------------------------
    try:
        _, d = post(f"{a.api}/search/bm25", {"query": "sacred trust", "top_k": 5})
        t = d.get("tiles", [])
        check("keyword /search/bm25 returns results", len(t) > 0,
              f"{len(t)} tiles, top score {float(t[0]['score']):.3f}" if t else "0 tiles")
    except Exception as e:
        check("keyword /search/bm25 returns results", False, f"{e}")

    # ---- 4. Embedding server: a REAL embed, not /health -------------------
    try:
        _, d = post(f"{a.embed}/v1/embeddings",
                    {"input": "production validation probe", "model": "Qwen/Qwen3-Embedding-8B"})
        dim = len(d["data"][0]["embedding"])
        check("embedding server performs a REAL embed", dim == 4096,
              f"returned {dim}-dim vector (a 200 on /health does NOT prove this)")
    except Exception as e:
        check("embedding server performs a REAL embed", False, f"{e}")

    # ---- 5. Deprecated route fails LOUD, with a pointer -------------------
    code, body = post_status(f"{a.api}/v2/search", {"query": "x", "top_k": 2})
    detail = body.get("detail", {}) if isinstance(body, dict) else {}
    points_at = detail.get("use_instead") if isinstance(detail, dict) else None
    unreachable = isinstance(detail, dict) and detail.get("unreachable")
    check("deprecated /v2/search fails loud", code == 410 and points_at == "POST /search",
          (f"service unreachable: {unreachable}" if unreachable else
           f"HTTP {code}, use_instead={points_at!r} (must not silently serve the 4.59% shadow)"))

    # ---- 6. Adaptive is SUPPORTED, not collateral damage -----------------
    try:
        _, d = post(f"{a.api}/v2/search/adaptive",
                    {"query": "how do I ingest a document into ISMA", "top_k": 5})
        t = d.get("tiles", d.get("results", []))
        top = float(t[0].get("score", 0)) if t else 0.0
        check("/v2/search/adaptive still supported", len(t) > 0 and top > 0.4,
              f"{len(t)} tiles, top {top:.3f} — V1-based, must survive the deprecation")
    except Exception as e:
        check("/v2/search/adaptive still supported", False, f"{e}")

    # ---- 7. Memory governance: superseded tiles are excluded -------------
    try:
        d = gql(a.weaviate, '{ Aggregate { ISMA_Quantum(where:{path:["is_superseded"],'
                            'operator:Equal,valueBoolean:true}) { meta { count } } } }')
        sup = d["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
        _, r = post(f"{a.api}/search", {"query": "what do we know about sacred trust", "top_k": 25})
        leaked = [t for t in r.get("tiles", []) if t.get("is_superseded") is True]
        check("superseded tiles excluded from default search", sup > 0 and not leaked,
              f"{sup:,} tiles flagged superseded; {len(leaked)} leaked into a top-25 result (must be 0)")
    except Exception as e:
        check("superseded tiles excluded from default search", False, f"{e}")

    # ---- 8. Ingest round-trip: verified by FILTER, with cleanup ----------
    marker = f"zqvalidate{uuid.uuid4().hex[:10]}"
    src = f"/__validate__/{marker}.md"
    oid = str(uuid.uuid4())
    try:
        body = {"class": "ISMA_Quantum", "id": oid, "properties": {
            "content": f"{marker} production validation round-trip tile.",
            "source_file": src, "scale": "search_512", "is_superseded": False}}
        req = urllib.request.Request(f"{a.weaviate}/v1/objects", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)

        # PRESENCE is a filter question — never an unfiltered ranked query.
        d = gql(a.weaviate, '{ Get { ISMA_Quantum(limit:5, where:{path:["source_file"],'
                            'operator:Equal,valueText:"%s"}) { doc_hash } } }' % src)
        present = len(d["data"]["Get"]["ISMA_Quantum"] or [])
        check("write round-trip verified by FILTER", present == 1,
              f"{present} tile present for source_file (store accepts writes; "
              f"NOT inferred from disk % or a log line)")
    except Exception as e:
        check("write round-trip verified by FILTER", False, f"{e}")
    finally:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{a.weaviate}/v1/objects/ISMA_Quantum/{oid}", method="DELETE"), timeout=30)
        except Exception:
            pass
        try:
            d = gql(a.weaviate, '{ Get { ISMA_Quantum(limit:5, where:{path:["source_file"],'
                                'operator:Equal,valueText:"%s"}) { doc_hash } } }' % src)
            left = len(d["data"]["Get"]["ISMA_Quantum"] or [])
        except Exception:
            left = -1
        check("validation left no residue", left == 0, f"{left} probe tiles remain (must be 0)")

    # ---- 10. Fixture residue left by OTHER verification runs --------------
    # Check 8 cleans up its own probe in a `finally`, which does not run if the
    # process is SIGKILLed; verify_authority_filter.py and disk_headroom_canary.sh
    # write fixtures too. A killed run therefore leaves real objects in the
    # PRODUCTION class and nothing notices. This is that notice.
    #
    # THREE THINGS THIS CHECK GETS WRONG IF WRITTEN NAIVELY, all found in review:
    #
    # 1. A BOUNDED PAGE CAN PROVE PRESENCE, NEVER ABSENCE. The token prefilters
    #    match ordinary corpus in bulk -- `source_file ~ *validate*` matches 535
    #    objects, because `/__validate__/` and .../vllm_engine/validate.py both
    #    reduce to the token "validate". Reading one limit:200 page and calling
    #    zero "clean" certifies absence from 37% of the candidates. So every
    #    prefilter is paged to its TERMINAL page before any zero is admissible.
    #
    # 2. THE INVENTORY OF EPHEMERAL WRITERS IS NOT SELF-EVIDENT. /__canary__/ was
    #    missing from the first version even though the canary is a known
    #    protected-class writer: it POSTs then deletes only on a successful POST,
    #    so a SIGKILL in that window leaves exactly this residue.
    #
    # 3. A CONTROL MUST EXERCISE THE DETECTOR'S OWN QUERY SHAPE. Proving
    #    `scale`+Equal works says nothing about `authority`/`source_file`+Like on a
    #    tokenized property. If Like silently returned empty for one property, the
    #    old control passed and every zero was certified false.
    PAGE = 200
    PAGE_CAP = 100_000

    def _page_all(prop, prefilter):
        """Page a token prefilter to its terminal page. Bounded pages prove
        presence; only a terminal page can support a claim of absence."""
        rows, off = [], 0
        while True:
            d = gql(a.weaviate, '{ Get { ISMA_Quantum(limit:%d, offset:%d, where:{path:["%s"],'
                                'operator:Like,valueText:"%s"}) { source_file authority } } }'
                                % (PAGE, off, prop, prefilter))
            page = d["data"]["Get"]["ISMA_Quantum"] or []
            rows.extend(page)
            if len(page) < PAGE:
                return rows
            off += PAGE
            if off > PAGE_CAP:
                raise RuntimeError(f"prefilter {prop}~{prefilter} exceeded {PAGE_CAP} rows")

    DETECTORS = (
        # label, property, token prefilter, exact substring, same-shape control token
        ("authority-filter fixture", "authority",   "*authority_filter_fixture*", "__authority_filter_fixture__", "*a*"),
        ("authority-filter source",  "source_file", "*fixture*",                  "/__fixture__/",                "*md*"),
        ("validation round-trip",    "source_file", "*validate*",                 "/__validate__/",               "*md*"),
        ("disk-canary probe",        "source_file", "*canary*",                   "/__canary__/",                 "*md*"),
    )

    residue, blind = {}, []
    for label, prop, prefilter, exact, ctl_token in DETECTORS:
        try:
            # same-shape liveness: identical property AND operator as the detector
            c = gql(a.weaviate, '{ Get { ISMA_Quantum(limit:1, where:{path:["%s"],'
                                'operator:Like,valueText:"%s"}) { source_file } } }' % (prop, ctl_token))
            if not (c["data"]["Get"]["ISMA_Quantum"] or []):
                blind.append(f"{label}({prop}+Like)")
                continue
            rows = _page_all(prop, prefilter)
            residue[label] = sum(1 for r in rows if exact in (r.get(prop) or ""))
        except Exception as e:
            residue[label] = f"ERROR: {e}"

    check("residue detectors are live (same property+operator as each probe)",
          not blind, f"blind detectors: {blind or 'none'} "
                     f"(a blind detector cannot certify a zero)")
    if not blind:
        clean = all(v == 0 for v in residue.values())
        check("no fixture residue in the production class", clean,
              f"{residue} (all must be 0; exhaustively paged, exact-string counted)")

    # ---- 9. Liveness of the units the path depends on --------------------
    try:
        units = ["isma-query-api", "isma-embedding", "isma-md-corpus-watch"]
        out = subprocess.run(["systemctl", "--user", "is-active"] + units,
                             capture_output=True, text=True, timeout=30).stdout.split()
        check("service units active", all(x == "active" for x in out) and len(out) == len(units),
              f"{dict(zip(units, out))}")
    except Exception as e:
        check("service units active", False, f"{e}")

    print()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"  {passed}/{total} checks passed")
    if passed != total:
        print("\n  FAILED:")
        for n, ok, o in RESULTS:
            if not ok:
                print(f"    - {n}: {o}")
        return 1
    print("  ALL PRODUCTION OBSERVATIONS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
