# How to correct a wrong document in ISMA — memory governance and supersession

**Owner:** weaver · **Measured 2026-07-30** · Full spec: `isma-core/MEMORY_GOVERNANCE.md`.
This is the procedure for making a correction actually beat the stale original in the fleet's memory.

## How to correct a document that is wrong in ISMA

**Edit the markdown file, then re-ingest it.** That is the whole intended operation — you do not delete
tiles, and you do not edit the store by hand.

```bash
cd $ISMA_HOME
PYTHONPATH=$ISMA_HOME WEAVIATE_URL=http://localhost:8088 \
EMBEDDING_URL=http://localhost:8089/v1/embeddings NEO4J_URI=bolt://localhost:7689 \
python3 isma/scripts/ingest_md_file.py <absolute-path-to.md>
```

## ⚠ FIXING THE FILE DOES NOT YET FIX THE MEMORY — hand-supersede until PR#11 merges

**Current state, verified 2026-07-30: supersede-on-reingest is isma-core PR#11 and is NOT MERGED.** With the
running ingest script, **re-ingesting leaves the old version co-current and fully retrievable** — its
`is_superseded` is never set. Observed live: a Taey-facing operating document held **25 tiles across 2
versions**, the older one carrying guidance already known to be wrong; another document had **40 stale tiles
across multiple co-current versions**. A correction that does not supersede is not a correction — **it is a
second opinion competing with the original at equal weight.**

So until PR#11 lands, after any re-ingest you must **hand-supersede the prior versions**:

1. Query the store for all tiles of that `source_file` and group them by `doc_hash`.
2. For every tile whose `doc_hash` is *not* the new one, PATCH
   `is_superseded=true`, `superseded_by=<new doc_hash>`, `correction_status="corrected"`.
3. **Verify: exactly ONE current `doc_hash` remains** for that `source_file`. That check is the proof, not
   the PATCH count. (Compare full hashes — a 12-character prefix compared against a stored 16-character
   hash will report a false mismatch.)

## What supersession does and does not do

The **exclusion** property is live and is the load-bearing safety guarantee: **174,227 tiles** carry
`is_superseded=true`, and the default `/search` filter excludes every one of them, so a stale draft cannot
outrank its correction. The filter is `is_superseded NotEqual true`, which **still returns tiles where the
flag is unset** (verified — it degrades gracefully). Unset is therefore *not* the same as hidden: an
un-superseded old version stays fully retrievable.

The **provenance pointer** is only partial. In a 200-tile sample of superseded tiles, **0% carried the
`superseded_by` pointer** — the historical corpus was superseded without it, and only recent writes and
hand-stamped corrections populate it. Honest statement: *exclusion works everywhere; "what superseded this,
and on what evidence" is answerable only for recent writes.* Backfill is measured feasible (8 of 8 sampled
source_files have a resolvable current successor) but is **not executed** — it is a ~174k-tile production
mutation awaiting an explicit go.

## Never do these when correcting an ISMA document

- **Never delete tiles to "fix" a wrong document.** Supersede it. The prior position must stay retrievable
  for audit — that is the whole point of a memory of record.
- **Never set `is_superseded=true` by hand without a provenanced refuter** — who declared it wrong, against
  what source, on what date. An unexplained supersession is indistinguishable from censorship of the record.
- **Never assume the correction propagated because you edited the file.** Verify one current `doc_hash`.
- **Never hand-edit fields in the store that came from an ingest** — re-ingest is the write path.

## If supersession is not firing

Notify weaver and read `isma-core/MEMORY_GOVERNANCE.md` first. **Supersede not firing on re-ingest is a
known BUG** with the fix in isma-core PR#11 — weaver owns it. A human or model confidently asserting a fact
that *is* correctly marked superseded is a **training gap**, not an infrastructure bug.
