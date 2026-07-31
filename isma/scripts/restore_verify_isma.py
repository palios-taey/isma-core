#!/usr/bin/env python3
"""Prove an ISMA backup is RESTORABLE — by restoring it and querying it.

A backup directory of the right size proves a copy happened. It does not prove
the store comes up, that objects are readable, that the inverted index survived,
or that governance flags are intact. The only evidence for those is a restore, so
this performs one in a scratch instance on a spare port and tears it down after.

Why this is Python and not a shell script: the bash version reported
NOT RESTORABLE for a backup that had already been proven restorable by hand. The
GraphQL query never reached the server — it was mangled escaping a query inside a
JSON body inside a double-quoted bash string. That is a defect FAMILY, not an
instance, so the fix is to stop hand-escaping rather than to escape more
carefully. Caught only because there was a known-good answer to check the new
instrument against.

Two false-green traps this deliberately avoids:
  * It requires the container to be RUNNING as well as the port to answer. A
    container that dies at startup still leaves a docker-proxy binding, so a bare
    readiness probe returns 200 for an instance that never ran. Observed.
  * It restores a COPY, never the backup itself — Weaviate writes to its data
    directory on startup (WAL recovery, LSM compaction), so an in-place test
    would mutate the artifact being protected.

Usage:
    restore_verify_isma.py <backup-dir> [--expect N] [--port 8091]
    restore_verify_isma.py <backup-dir> --self-test   # negative control

Exit 0 only if every check passes.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CLASS = "ISMA_Quantum"
IMAGE = "cr.weaviate.io/semitechnologies/weaviate:latest"


def gql(port, query, timeout=90):
    req = urllib.request.Request(
        f"http://localhost:{port}/v1/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def sh(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def container_running(name):
    r = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=30)
    return r.stdout.strip() == "true"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backup")
    ap.add_argument("--expect", type=int, default=0)
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--scratch", default=None)
    ap.add_argument("--self-test", action="store_true",
                    help="corrupt the scratch copy on purpose; the run MUST go red")
    a = ap.parse_args()

    backup = Path(a.backup)
    data = backup / "data"
    if not data.is_dir():
        print(f"FAIL: {data} not found — not a backup from backup_isma_store.sh")
        return 2

    scratch = Path(a.scratch or f"/var/spark/isma-restore-verify-{int(time.time())}")
    name = f"isma-restore-verify-{scratch.name}"
    results = []

    def check(label, ok, note):
        results.append((label, bool(ok)))
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         {note}")

    print(f"RESTORE VERIFICATION: {backup}"
          + ("   [SELF-TEST: copy will be corrupted on purpose]" if a.self_test else ""))
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        r = sh(["rsync", "-a", f"{data}/", f"{scratch}/"], timeout=3600)
        if r.returncode != 0:
            print(f"FAIL: could not copy backup to scratch: {r.stderr[:200]}")
            return 1

        if a.self_test:
            # Negative control: destroy the LSM segments so a working verifier
            # MUST report failure. If a corrupted copy still passes, the verifier
            # is not measuring anything.
            killed = 0
            for p in scratch.rglob("*.db"):
                try:
                    p.write_bytes(b"")
                    killed += 1
                except Exception:
                    pass
            print(f"  self-test: truncated {killed} store segment files")

        sh(["docker", "rm", "-f", name], timeout=60)
        r = sh(["docker", "run", "-d", "--name", name, "-p", f"{a.port}:8080",
                "-v", f"{scratch}:/var/lib/weaviate",
                "-e", "PERSISTENCE_DATA_PATH=/var/lib/weaviate",
                "-e", "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true",
                "-e", "DEFAULT_VECTORIZER_MODULE=none",
                "-e", "QUERY_MAXIMUM_RESULTS=700000",
                "-e", "CLUSTER_HOSTNAME=node1", IMAGE], timeout=180)
        if r.returncode != 0:
            check("instance starts", False, f"docker run failed: {r.stderr[:160]}")
            return 1

        ready = False
        for _ in range(60):
            # BOTH conditions: a dead container still holds the port binding.
            if container_running(name):
                try:
                    urllib.request.urlopen(
                        f"http://localhost:{a.port}/v1/.well-known/ready", timeout=3)
                    ready = True
                    break
                except Exception:
                    pass
            time.sleep(2)
        check("instance came up (container RUNNING and ready)", ready,
              "container alive and answering" if ready else
              "never became ready — the store did not load")
        if not ready:
            return 1

        try:
            n = gql(a.port, "{ Aggregate { %s { meta { count } } } }" % CLASS
                    )["data"]["Aggregate"][CLASS][0]["meta"]["count"]
        except Exception as e:
            n = None
            print(f"         (count query error: {str(e)[:90]})")
        if a.expect:
            check("tile count matches expected", n == a.expect,
                  f"{n if n is not None else 'no answer'} vs expected {a.expect}")
        else:
            check("tile count plausible", bool(n and n > 1_000_000),
                  f"{n if n is not None else 'no answer'} tiles")

        try:
            got = gql(a.port,
                      '{ Get { %s(limit:1, where:{path:["content"],operator:Like,'
                      'valueText:"*ISMA*"}) { content } } }' % CLASS
                      )["data"]["Get"][CLASS] or []
            clen = len((got[0].get("content") or "")) if got else 0
        except Exception:
            clen = 0
        check("object content readable", clen > 20,
              f"{clen} chars read from a stored object"
              if clen else "could not read any object content")

        try:
            hits = gql(a.port, '{ Get { %s(limit:3, bm25:{query:"sacred trust"}) '
                               '{ doc_hash } } }' % CLASS)["data"]["Get"][CLASS] or []
        except Exception:
            hits = []
        check("BM25 works (inverted index restored)", len(hits) > 0,
              f"{len(hits)} hits" if hits else "no hits — inverted index did not restore")

        try:
            sup = gql(a.port, '{ Aggregate { %s(where:{path:["is_superseded"],'
                              'operator:Equal,valueBoolean:true}) { meta { count } } } }' % CLASS
                      )["data"]["Aggregate"][CLASS][0]["meta"]["count"]
        except Exception:
            sup = 0
        check("memory governance intact", sup > 0,
              f"{sup:,} superseded flags preserved" if sup else "no superseded flags found")

    finally:
        sh(["docker", "rm", "-f", name], timeout=60)
        shutil.rmtree(scratch, ignore_errors=True)
        print(f"  cleanup: scratch instance and copy removed ({scratch})")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n  {passed}/{len(results)} checks passed")
    restorable = passed == len(results)

    if a.self_test:
        # In self-test the CORRECT outcome is failure. A pass here means the
        # verifier cannot detect a destroyed backup and must not be trusted.
        if restorable:
            print("  SELF-TEST FAILED: a deliberately corrupted copy was reported "
                  "RESTORABLE. This verifier does not measure anything.")
            return 1
        print("  SELF-TEST PASSED: corruption was correctly detected as NOT RESTORABLE")
        return 0

    print("  RESTORABLE — all checks passed" if restorable
          else "  NOT PROVEN RESTORABLE — see FAIL lines above")
    return 0 if restorable else 1


if __name__ == "__main__":
    sys.exit(main())
