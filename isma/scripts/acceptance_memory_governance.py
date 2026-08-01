#!/usr/bin/env python3
"""Adversarial acceptance for the memory-governance fix. Runs against a SCRATCH
Weaviate, never production.

It proves the three properties the fix exists to guarantee. Each case is written
as an attack: the two orderings below BOTH destroyed the document under the old
dedup, and the third is the idempotency claim that a false "already ingested"
used to make dishonestly.

  CASE 1  path-move repair, ingest -> supersede
          Old: dedup matched the stale-path tiles by hash, so the ingest was a
          NO-OP reporting success; the following supersede then left ZERO live
          tiles. The document disappeared while the operation logged success.

  CASE 2  path-move repair, supersede -> ingest
          Old: dedup was superseded-BLIND, so the just-superseded tiles still
          matched; the ingest skipped and the content stayed superseded-only —
          invisible to the default `is_superseded NotEqual true` filter.

  CASE 3  re-ingest of UNCHANGED content at the SAME path
          Must supersede nothing, write nothing, and say `already-live` — not
          "ingested". The old code returned True for this, which is how batch
          counters reported no-ops as successes.

The invariant under test, stated once: **no ingest/supersede sequence may leave a
document whose file exists on disk with zero live tiles.**

Nothing here deletes. `is_superseded=true` is the only mutation the fix performs,
so every step is reversible by flipping the flag back.

Usage:
    acceptance_memory_governance.py [--port 8092] [--keep]

Exit 0 only if every case passes.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

IMAGE = "cr.weaviate.io/semitechnologies/weaviate:latest"
CLASS = "ISMA_Quantum"


def sh(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def http(url, data=None, method=None, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


def live_count(base, doc_hash=None, source_file=None):
    """Count LIVE tiles by FILTER, never by search — presence is a filter question."""
    ops = ['{path:["is_superseded"],operator:NotEqual,valueBoolean:true}']
    if doc_hash:
        ops.append('{path:["doc_hash"],operator:Equal,valueText:"%s"}' % doc_hash)
    if source_file:
        ops.append('{path:["source_file"],operator:Equal,valueText:"%s"}' % source_file)
    q = ('{ Aggregate { %s(where:{operator:And,operands:[%s]}) { meta { count } } } }'
         % (CLASS, ",".join(ops)))
    r = http(f"{base}/v1/graphql", {"query": q})
    return r["data"]["Aggregate"][CLASS][0]["meta"]["count"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--keep", action="store_true", help="leave the scratch instance up")
    ap.add_argument("--negative-control", action="store_true",
                    help="restore the OLD hash-only, superseded-blind dedup; the run MUST go red. "
                         "A suite that cannot fail proves nothing.")
    a = ap.parse_args()

    prod = os.environ.get("WEAVIATE_URL")
    if not prod:
        print("FAIL: set WEAVIATE_URL (the PRODUCTION store) — its schema is cloned read-only")
        return 2
    if not os.environ.get("EMBEDDING_URL"):
        print("FAIL: set EMBEDDING_URL — ingest embeds for real; this is not mocked")
        return 2

    name = f"isma-acceptance-{int(time.time())}"
    base = f"http://localhost:{a.port}"
    results = []

    def check(label, ok, note):
        results.append((label, bool(ok)))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         {note}")

    tmp = tempfile.mkdtemp(prefix="isma-acceptance-")
    try:
        # ---- scratch instance -------------------------------------------------
        sh(["docker", "rm", "-f", name], timeout=60)
        r = sh(["docker", "run", "-d", "--name", name, "-p", f"{a.port}:8080",
                "-v", f"{tmp}:/var/lib/weaviate",
                "-e", "PERSISTENCE_DATA_PATH=/var/lib/weaviate",
                "-e", "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true",
                "-e", "DEFAULT_VECTORIZER_MODULE=none",
                "-e", "CLUSTER_HOSTNAME=node1", IMAGE])
        if r.returncode != 0:
            print(f"FAIL: docker run: {r.stderr[:200]}")
            return 1
        for _ in range(60):
            try:
                http(f"{base}/v1/.well-known/ready", timeout=3)
                break
            except Exception:
                time.sleep(2)
        else:
            print("FAIL: scratch instance never became ready")
            return 1

        # Clone the REAL class definition rather than inventing one, so the test
        # cannot pass against a schema production does not have.
        schema = http(f"{prod}/v1/schema/{CLASS}")
        for k in ("vectorizer", "moduleConfig", "replicationConfig", "shardingConfig"):
            schema.pop(k, None)
        schema["vectorizer"] = "none"
        http(f"{base}/v1/schema", schema)

        # ---- ingest wired to the scratch store --------------------------------
        os.environ["WEAVIATE_URL"] = base
        import importlib
        import isma.config
        importlib.reload(isma.config)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import ingest_md_file as ing
        importlib.reload(ing)
        ing.WEAVIATE_URL = base

        if a.negative_control:
            # Reinstate the pre-fix semantics EXACTLY: any tile with this hash
            # counts, superseded or not, and path is ignored. If the cases below
            # still pass under this, they are not testing what they claim.
            def old_semantics(doc_hash_value, source_file):
                q = ('{ Get { %s(limit:1, where:{path:["doc_hash"],operator:Equal,'
                     'valueText:"%s"}) { doc_hash } } }' % (CLASS, doc_hash_value))
                items = http(f"{base}/v1/graphql", {"query": q})["data"]["Get"][CLASS] or []
                return (len(items), 0)      # "here" regardless of path -> caller skips
            ing.live_tiles_for = old_semantics
            print("  [negative control] old hash-only superseded-blind dedup restored\n")

        body = ("# Acceptance fixture\n\n"
                "This document exists to prove that a repair sequence cannot drive a "
                "document to zero live tiles. It is long enough to clear the minimum "
                "ingest length, and its content is deliberately unremarkable so that "
                "nothing about retrieval ranking is being asserted here — only "
                "presence, which is a filter question and never a ranking one.\n")
        stale_dir = Path(tmp) / "stale"; stale_dir.mkdir()
        canon_dir = Path(tmp) / "canonical"; canon_dir.mkdir()
        stale = stale_dir / "DOC.md"; stale.write_text(body)
        canon = canon_dir / "DOC.md"; canon.write_text(body)
        from isma.src.hmm.ids import content_hash
        H = content_hash(body)

        # ---- CASE 1: path-move, ingest -> supersede ---------------------------
        assert ing.ingest_file(stale) == ing.INGESTED, "fixture setup failed"
        before = live_count(base, doc_hash=H, source_file=str(stale))
        outcome = ing.ingest_file(canon)          # the repair, ordering 1
        at_canon = live_count(base, doc_hash=H, source_file=str(canon))
        at_stale = live_count(base, doc_hash=H, source_file=str(stale))
        total = live_count(base, doc_hash=H)
        check("CASE 1 — path-move (ingest then supersede) leaves the doc LIVE",
              outcome == ing.INGESTED and at_canon > 0 and at_stale == 0 and total > 0,
              f"outcome={outcome}; canonical={at_canon} live, stale={at_stale} live "
              f"(was {before}); total live={total} — old code reached ZERO here")

        # ---- CASE 2: path-move, supersede -> ingest ---------------------------
        # Fresh fixture; mark the stale-path tiles FIRST, then ingest canonically.
        body2 = body.replace("fixture", "fixture two")
        stale2 = stale_dir / "DOC2.md"; stale2.write_text(body2)
        canon2 = canon_dir / "DOC2.md"; canon2.write_text(body2)
        H2 = content_hash(body2)
        assert ing.ingest_file(stale2) == ing.INGESTED, "fixture setup failed"
        ing.supersede_prior_versions("__nothing__", "__none__")   # no-op guard
        ids = http(f"{base}/v1/graphql", {"query":
              '{ Get { %s(limit:100, where:{path:["doc_hash"],operator:Equal,valueText:"%s"})'
              ' { _additional { id } } } }' % (CLASS, H2)})["data"]["Get"][CLASS]
        for o in ids:
            http(f"{base}/v1/objects/{CLASS}/{o['_additional']['id']}",
                 {"class": CLASS, "properties": {"is_superseded": True}}, method="PATCH")
        pre_live = live_count(base, doc_hash=H2)
        outcome2 = ing.ingest_file(canon2)        # the repair, ordering 2
        post_live = live_count(base, doc_hash=H2, source_file=str(canon2))
        check("CASE 2 — path-move (supersede then ingest) leaves the doc LIVE",
              outcome2 == ing.INGESTED and post_live > 0,
              f"live before ingest={pre_live} (all superseded); outcome={outcome2}; "
              f"canonical live after={post_live} — old code SKIPPED and stayed at 0")

        # ---- CASE 3: idempotency, unchanged content, same path ----------------
        t_before = live_count(base, doc_hash=H, source_file=str(canon))
        outcome3 = ing.ingest_file(canon)
        t_after = live_count(base, doc_hash=H, source_file=str(canon))
        check("CASE 3 — re-ingest of unchanged content is a truthful no-op",
              outcome3 == ing.ALREADY_LIVE and t_after == t_before,
              f"outcome={outcome3} (must be '{ing.ALREADY_LIVE}', never '{ing.INGESTED}'); "
              f"tiles {t_before} -> {t_after}, superseded nothing")

        # ---- CASE 4: stale version live ALONGSIDE current content ------------
        # The migration case. Current content is already live at the path, and an
        # OLDER version of the same path is live too. Nothing needs writing, but
        # the stale version must still be retired — otherwise every
        # already-current document is permanently un-repairable.
        body4 = body.replace("fixture", "fixture four")
        p4 = canon_dir / "DOC4.md"; p4.write_text(body4)
        assert ing.ingest_file(p4) == ing.INGESTED, "fixture setup failed"
        H4_old = content_hash(body4)
        p4.write_text(body4 + "\nA later revision of the same document.\n")
        assert ing.ingest_file(p4) == ing.INGESTED, "fixture setup failed"
        H4_new = content_hash(p4.read_text())
        # Resurrect the old version to simulate the accumulated real-world state.
        olds = http(f"{base}/v1/graphql", {"query":
              '{ Get { %s(limit:100, where:{path:["doc_hash"],operator:Equal,valueText:"%s"})'
              ' { _additional { id } } } }' % (CLASS, H4_old)})["data"]["Get"][CLASS]
        for o in olds:
            http(f"{base}/v1/objects/{CLASS}/{o['_additional']['id']}",
                 {"class": CLASS, "properties": {"is_superseded": False}}, method="PATCH")
        pre_old = live_count(base, doc_hash=H4_old)
        outcome4 = ing.ingest_file(p4)          # unchanged content — the migration call
        post_old = live_count(base, doc_hash=H4_old)
        post_new = live_count(base, doc_hash=H4_new)
        check("CASE 4 — a no-op ingest still retires stale versions at the same path",
              outcome4 == ing.ALREADY_LIVE and pre_old > 0 and post_old == 0 and post_new > 0,
              f"outcome={outcome4}; stale live {pre_old} -> {post_old}; current live={post_new} "
              f"— without this, every already-current doc is permanently un-repairable")

        # ---- CASE 5: supersession must NOT cross documents --------------------
        # source_file is tokenization=word, so a GraphQL Equal on a path matches
        # by TOKEN and returns unrelated documents whose paths share tokens.
        # Superseding one document must never touch another.
        #
        # Self-contained fixtures on purpose: an earlier draft revised CASE 1's
        # document here, which legitimately superseded it and turned the shared
        # invariant red. A case that perturbs another case's fixture cannot tell
        # you which one failed.
        d5 = canon_dir / "DOC5.md"
        s5 = canon_dir / "DOC5_SIBLING.md"      # shares every path token but one
        d5.write_text(body.replace("fixture", "case five primary"))
        s5.write_text(body.replace("fixture", "case five sibling, unrelated"))
        assert ing.ingest_file(d5) == ing.INGESTED, "fixture setup failed"
        assert ing.ingest_file(s5) == ing.INGESTED, "fixture setup failed"
        H5_sib = content_hash(s5.read_text())
        sib_before = live_count(base, doc_hash=H5_sib)
        # revise ONLY the primary, forcing a supersede pass on its path
        d5.write_text(d5.read_text() + "\nRevision that triggers a supersede pass.\n")
        assert ing.ingest_file(d5) == ing.INGESTED, "fixture setup failed"
        H5_new = content_hash(d5.read_text())
        sib_after = live_count(base, doc_hash=H5_sib)
        check("CASE 5 — superseding one doc does not touch a sibling path",
              sib_before > 0 and sib_after == sib_before,
              f"sibling live {sib_before} -> {sib_after} (must be unchanged); "
              f"GraphQL Equal on source_file is token-based and returns other docs")

        # ---- invariant sweep --------------------------------------------------
        zero = [h for h in (H, H2, H4_new) if live_count(base, doc_hash=h) == 0]
        check("INVARIANT — no fixture document reachable at zero live tiles",
              not zero, "all fixtures have live tiles" if not zero
              else f"ZERO-LIVE for {zero} — the defect is NOT fixed")

    finally:
        if not a.keep:
            sh(["docker", "rm", "-f", name], timeout=60)
            # Weaviate writes its data dir as ROOT inside the container, so an
            # unprivileged rm -rf silently fails. Verify rather than announce:
            # claiming a cleanup that did not happen is the same false-success
            # this whole change exists to eliminate.
            sh(["rm", "-rf", tmp], timeout=60)
            if os.path.exists(tmp):
                rc = sh(["sudo", "-n", "rm", "-rf", tmp], timeout=60).returncode
                if rc != 0 or os.path.exists(tmp):
                    print(f"  CLEANUP INCOMPLETE — scratch data remains at {tmp} "
                          f"(root-owned by the container). Remove it manually.")
                else:
                    print("  cleanup: scratch instance and fixtures removed (needed sudo)")
            else:
                print("  cleanup: scratch instance and fixtures removed")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n  {passed}/{len(results)} cases passed")
    if a.negative_control:
        if passed == len(results):
            print("  NEGATIVE CONTROL FAILED — the suite passed against the KNOWN-BROKEN "
                  "dedup, so it does not detect the defect it claims to test.")
            return 1
        print(f"  NEGATIVE CONTROL OK — {len(results)-passed} case(s) went red against the "
              "old semantics, so the suite can actually fail.")
        return 0
    if passed == len(results) and results:
        print("  ACCEPTED — both near-miss orderings are safe; idempotency is honest")
        return 0
    print("  REJECTED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
