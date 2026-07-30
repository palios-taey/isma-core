# How to ingest a document into ISMA and verify the ingest actually worked

**Owner:** weaver · **Measured:** 2026-07-30 · This is the canonical ISMA ingestion procedure. Every
verification step below was run against the live store; the wrong methods named here were measured wrong,
not guessed.

## Ingesting a document into ISMA

To ingest a markdown document into ISMA, run the ISMA ingest script against the file:

```bash
cd $ISMA_HOME
PYTHONPATH=$ISMA_HOME \
WEAVIATE_URL=http://localhost:8088 \
EMBEDDING_URL=http://localhost:8089/v1/embeddings \
NEO4J_URI=bolt://localhost:7689 \
python3 isma/scripts/ingest_md_file.py <absolute-path-to.md>
```

Ingestion is also continuous: the ISMA corpus watcher (`isma-md-corpus-watch.service`) picks up watched
roots automatically, and the ISMA nightly ingest timer (`isma-nightly-ingest.timer`) runs at 03:00 daily.
Ingest writes to the V1 Weaviate class `ISMA_Quantum` only. Never ingest into any other class.

## How to verify an ISMA ingest worked

**The log line is NOT proof of ingestion.** The ingest log prints `inserted N/N`, but a log line is not
evidence that tiles persisted. On a full disk Weaviate goes READ-ONLY and the batch endpoint returns
HTTP 200 with per-object `FAILED` status, so reads keep working perfectly while every write silently
fails. Verifying an ISMA ingest requires two checks against the store itself:

**Check 1 — persistence.** Query the store directly for the document's `source_file` and confirm the tile
count matches what the ingest claimed:

```graphql
{ Get { ISMA_Quantum(where:{path:["source_file"],operator:Equal,valueText:"<your-path>"}) { doc_hash scale } } }
```

**Check 2 — the text is indexed and retrievable.** Run a BM25 term search **restricted to your document's
`source_file`**, and confirm it returns matches. The filter is what makes this deterministic:

```graphql
{ Get { ISMA_Quantum(limit:10, bm25:{query:"<rare-term-from-the-doc>"},
    where:{path:["source_file"],operator:Equal,valueText:"<your-path>"}) { scale _additional { score } } } }
```

Matches returned = the text is in the BM25 index and retrievable. Zero matches = the ingest did not land.

## NEVER verify an ISMA ingest with an unfiltered ranked search — neither semantic NOR BM25

**A ranking surface cannot answer "is my document present."** Rank depends on the other 1.6M tiles, not on
whether your document is there — so *any* unfiltered ranked query will report a correct ingest as failed.
This holds for **both** ISMA search surfaces, and both were measured on 2026-07-30:

- **Semantic `/search`:** a correctly-ingested document scored 0.35 at ranks 9–11 on a *verbatim* query
  while established documents held 0.62–0.65.
- **BM25 `/search/bm25`:** a correctly-ingested document's genuinely rare term scored 0.73 while other
  documents scored 2.90–3.99 on that same term — so it was ABSENT from the top 10 despite being fully
  indexed (a `source_file`-filtered BM25 on the identical term returned its 4 matching tiles immediately).

The trap is that **"rare in the world" is not "rare in this corpus."** ISMA holds ~70% conversation
transcripts, so every term you chose *because* it was distinctive has usually already been discussed at
length in transcripts and prior drafts that will out-rank your new document on it.

A second, independent ranking effect: a **broad index document covering several topics is out-ranked on
every one of its topics by a topic-focused document**, because both surfaces reward term density. Measured:
a 4-process ISMA index document was ABSENT from the top 50 on BM25 queries using exact terms from its own
text, while single-topic documents on those terms scored 5.05–6.88. **If you want a procedure to be findable
by question, give it its own focused document** — a focused procedure document hit 4 of 6 natural phrasings
(twice at rank 0, score 0.65) where the broad index hit none.

**So: verify presence with FILTERS (Checks 1 and 2 above, both deterministic and corpus-independent).
Judge findability with ranking — never the reverse.**

## Ingest failure modes to check first

- **Disk full → Weaviate READ-ONLY.** Writes fail silently while reads succeed. Check disk before anything.
- **HTTP 200 with per-object FAILED.** The batch endpoint reports success at the request level; the failure
  is per object. The insert counter used to score those as success (fixed in isma-core PR#11).
- **`inserted N/M` with N < M** — a partial insert, not a success.
- **Zero tiles present in the store** for your `source_file` — the ingest did not land.

## Never do these when ingesting into ISMA

- Never report an ISMA ingest as done from the log line alone.
- Never ingest a document containing a live credential, API key, or presigned URL — scrub it first.
- Never ingest into any Weaviate class other than `ISMA_Quantum` (V1).
- Never mark tiles superseded by hand without a provenanced refuter (who declared it wrong, against what
  source, on what date). To correct an ISMA document, edit the markdown and re-ingest it; re-ingest
  supersedes the prior version (isma-core PR#11). Never delete tiles to fix a wrong document — supersede
  it, so the prior position stays retrievable for audit.

**On failure, notify weaver.** Review `isma-core/KNOWN_FINDINGS.md` first: disk / read-only / wrong route
is a BUG that weaver fixes; "I ingested it and never verified it" is a training gap.
