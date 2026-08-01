# ISMA production capability map

**Measured 2026-07-30 against the live system.** Every number here came from a command run at authoring
time — no capability is claimed by name. Registers: **[Observed]** = measured · **[Inferred]** = my reading
of the measurement. Reproduce any line with the commands in §6.

---

## 1. What ISMA does in production

**[Observed]** ISMA is the semantic memory over authored prose and conversation history: **1,606,765 tiles**
in the live class `ISMA_Quantum`, hybrid BM25 + dense (Qwen3-Embedding-8B, 4096-dim, confirmed by a real
embed returning 4096 dimensions), multi-scale (`search_512` / `context_2048` / `full_4096` / `rosetta`),
served over HTTP on `:8095`. It answers *"what do we know / what did we say about X"*.

**[Observed] What it is not built for — a design note, not a defect.** It cannot return THE one
authoritative value for a known key. `/search` is a **similarity ranker**: it returns what most *resembles*
the question, which is systematically not the same as the one right answer. Measured twice, including a case
where it failed to return its own canonical retrieval rule. Every ID-addressable route is keyed by
`content_hash`, which changes on every edit — backwards from what canonical lookup needs (a stable key whose
*value* changes).

## 2. Endpoints — canonical vs deprecated, by live probe

All four probed with the identical query, 2026-07-30:

| Endpoint | Status | Live evidence |
|---|---|---|
| `POST :8095/search` | **CANONICAL** — prose/semantic | 200, 3 tiles, top score **0.455**. Serves the full 1,606,765-tile V1 class. |
| `POST :8095/search/bm25` | **CANONICAL** — exact-term/keyword | 200, 3 tiles, top **3.366**. The reliable path for "did my specific document land" when combined with a `source_file` filter. |
| `GET :8095/document/{content_hash}/text` | **CANONICAL** — expansion | Works for both `.md` and transcript sources. |
| `POST :8095/v2/search`, `/v2/search/semantic` | **DEPRECATED — do not use for prose** | 200, but top score **0.085 vs V1's 0.455 on the same query**. Queries `ISMA_Quantum_v2` = **73,809 tiles = 4.59%** of V1. |
| `POST :8095/v2/search/adaptive` | **SUPPORTED — V1 + overlay** | **Correction (see §2a).** Measured **0.650–0.700**, equal to canonical; finds documents authored today that have zero tiles in the v2 class. Two live production consumers. |
| `POST :8095/search/hmm`, `/search/motif` | **DEPRECATED for prose** | HMM-gated. Authored prose is `hmm_enriched=false`, so the gate filters out exactly the high-value layer. |

### 2a. Correction to this file, 2026-07-30

**The row above previously read `/v2/search` (and `/v2/*`) — DEPRECATED, which wrongly condemned `/v2/search/adaptive` along with it. That was my error and this supersedes it.** Adaptive is **V1-based with a V2 overlay**, not the shadow: its implementation states *"All non-relational strategies use V1 hybrid_retrieve_hmm as the base"*, imports V1 `get_retrieval`, and sets `V1_CLASS = ISMA_Quantum`. Decisive probe: a document authored today, verified by direct GraphQL to have **zero tiles** in `ISMA_Quantum_v2`, is returned by adaptive at **rank 0**. Two live production consumers depend on it and were getting canonical-quality results the whole time. **Deprecating all of `/v2/*` as this file originally implied would have broken them** — 46% of ISMA query traffic runs through v2 routes, 160 of 237 on adaptive.

**[Observed] The deprecation is harmful, not cosmetic.** Plain `/v2/search` returns a plausible ranked answer built on
4.59% of the corpus, at HTTP 200, with nothing signalling degradation — a near-miss that *looks like success*,
which is the worst failure mode. This is not hypothetical: three real research queries hit `/v2/search` and
`/v2/search/semantic` on 2026-07-28 and 2026-07-29 before the caller discovered `/search` by probing `/` and
`/health`. **Someone had to find the correct endpoint by exploration after three degraded answers.**

## 3. Measured capacity

**[Observed]** Semantic `/search`, `top_k=10`, 18 sequential queries against the live store:

| Metric | Semantic `/search` | Keyword `/search/bm25` |
|---|---|---|
| p50 | **254 ms** | **205 ms** |
| p95 | 767 ms | 756 ms |
| min / max | 89 / 790 ms | — |

**[Observed] Concurrency** — 6 workers × 3 rounds = 18 requests: **7.8 req/s**, p50 479 ms, p95 1399 ms,
all requests returned tiles (no errors, no empty results under load).

**[Observed] `top_k` scaling** is sub-linear: `top_k=5` → 750 ms, `25` → 783 ms, `50` → 1023 ms. **[Inferred]**
Retrieval cost is dominated by the embed + ANN traversal rather than by result count, so the GO-DEEP default of
`top_k>=25` costs almost nothing over `top_k=5`. Deep queries are close to free; there is no capacity argument
for querying shallowly.

## 4. Liveness signal — and what a green does NOT prove

**[Observed] All green at authoring time:** `isma-query-api`, `isma-embedding`, `isma-md-corpus-watch` all
**active AND enabled** (autostart verified, not assumed); `embed-canary.timer` and `isma-nightly-ingest.timer`
both scheduled; `:8095`, `:8089`, `:8088` all HTTP 200.

**[Observed] Two known false-greens — check these instead of the health flag:**

1. **`:8089/health` can return 200 while every real embed 500s.** That exact false-green caused a two-day
   silent-stale ingestion outage. The canary exists *because* the flag lied: `embed-canary.service` POSTs a
   **real embed** every 5 minutes and restarts on non-200. Verified live at authoring time: a real embed
   returned **4096 dimensions**.
2. **Weaviate goes READ-ONLY at a disk threshold that is UNVERIFIED (see 4a).** Reads keep working perfectly while every write silently fails,
   so search looks healthy while the corpus stops growing. **Do not infer writability from the disk
   percentage — probe it.** Verified by writing and deleting a probe object: the store is writable now.

### 4a. Correction 2026-08-01 — the 90% read-only threshold is UNVERIFIED

**UNVERIFIED — inherited figure, never observed.** Measured 2026-08-01: the store was at **92% and still accepting writes** (live write probe succeeded, node HEALTHY, no read-only line in the container log, and `DISK_USE_READONLY_PERCENTAGE` is not set). So the real threshold on this deployment is unknown and this number should not be planned against until it is measured. What IS verified: past whatever the real threshold is, **reads keep working perfectly while every write silently fails** — so the check is always a WRITE probe, never a read.

**[Observed] ⚠ Current headroom is thin.** The Weaviate store lives at `/var/spark/weaviate-isma` on
`/dev/nvme1n1p2`, which is **89% used (203 G free)** — one point below the read-only threshold. Writes
succeed today. **[Inferred]** At 90% ingestion begins failing silently while every dashboard stays green;
this is the single most likely near-term production failure and it is invisible from the read path.

## 5. Assert the artifact, not the name

Run this, not a health check:

```bash
# 1. ingest a uniquely-worded doc
python3 isma/scripts/ingest_md_file.py /tmp/probe.md      # expect "inserted N/N", N==N
# 2. prove PERSISTENCE in the store (the log line is not proof)
#    GraphQL: ISMA_Quantum where source_file == /tmp/probe.md   -> expect N tiles
# 3. prove the text is INDEXED — BM25 filtered to that same source_file -> expect matches
# 4. clean up by exact object id
```

**Verify presence with FILTERS, never with an unfiltered ranked query — neither `/search` nor
`/search/bm25`.** Rank is determined by the other 1.6M tiles, not by whether your document is present, so both
will report a *correct* ingest as failed. Measured: a correct ingest sat at semantic rank 9–11 (0.35 vs
established 0.62–0.65), and the same document's genuinely rare term scored BM25 0.73 against competitors at
2.90–3.99 — absent from the top 10 while fully indexed. **"Rare in the world" is not "rare in this corpus":**
~70% of ISMA is conversation transcripts that already discuss any term distinctive enough to choose.

## 6. Reproduce every number above

```bash
# corpus size
curl -s localhost:8088/v1/graphql -d '{"query":"{ Aggregate { ISMA_Quantum { meta { count } } } }"}'
# endpoint comparison (same query, all routes)
curl -s -X POST localhost:8095/search      -d '{"query":"<q>","top_k":3}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8095/v2/search   -d '{"query":"<q>","top_k":3}' -H 'Content-Type: application/json'
# real embed (what /health cannot tell you)
curl -s -X POST localhost:8089/v1/embeddings -d '{"input":"probe","model":"Qwen/Qwen3-Embedding-8B"}' -H 'Content-Type: application/json'
# disk headroom vs the 90% read-only gate
df -h /var/spark
# liveness
systemctl --user is-active isma-query-api isma-embedding isma-md-corpus-watch
systemctl --user list-timers | grep -E 'isma|canary'
```

*Every figure was measured 2026-07-30; probe objects were deleted after use. If a number here disagrees with
the live system, the live system is right and this file is stale — re-measure and update it.*
