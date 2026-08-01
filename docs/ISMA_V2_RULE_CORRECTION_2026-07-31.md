# CORRECTED ISMA routing rule — replaces the "NEVER /v2/*" wording

**By:** weaver (ISMA owner) · **2026-07-31** · Supersedes the NO-HMM / V1-ONLY clause as it was
written in the always-loaded operator context files that carry ISMA routing guidance (five of
them at the time).

> **Re-homed into this public repo 2026-08-01.** It previously lived in an operator-local
> `reports/` directory inside a stale private tree and was **untracked by any repo** — a single
> disk fault from gone, while an always-loaded context file cited it as the "full evidence" for a
> rule every seat follows. That is the same disconnection failure this repo's `CLAUDE.md` uses as
> its worked example, in the same directory. Evidence for a live rule belongs where the rule's
> readers can actually reach it.

## Why this exists

The old wording said **"NEVER `/v2/*`"** and **"NEVER `isma_adaptive_search` (routes to that
shadow)"**. **That was mine and it was wrong about `adaptive`.** It was retracted in isma-core
PR#20 (merged) — but a retraction living in a merged PR cannot beat a rule that is re-injected
into every seat's context on every prompt. A seat then read the standing rule, pattern-matched
`/v2/search/adaptive`, and re-derived my error confidently. That is a doc-propagation defect,
not a memory failure, and the fix is to change the text every seat actually loads.

## Drop-in replacement paragraph

> **THREE RULES: NO-HMM / V1-FIRST** — use `/search` (V1 `ISMA_Quantum`, full ~1.6M corpus) or
> MCP `isma_search` for prose. **`POST /v2/search` is DEPRECATED and now returns HTTP 410** with
> a pointer to `/search`: it queried the `ISMA_Quantum_v2` shadow (73,809 tiles = **4.59%** of the
> corpus, frozen), answering *plausibly* from a fraction of what we know — measured 0.056 vs
> `/search` 0.650 on the same query. **`/v2/search/adaptive` (and MCP `isma_adaptive_search`) are
> SUPPORTED, not deprecated** — corrected 2026-07-31: adaptive is **V1-based with a V2 overlay**
> (its implementation uses V1 `hybrid_retrieve_hmm` as the base and sets `V1_CLASS = ISMA_Quantum`),
> measured **0.650–0.700**, equal to canonical, and it returns documents authored today that have
> **zero tiles** in the v2 class. Two live production consumers depend on it. Still **NEVER**
> `/search/hmm`, `/search/motif`, or `enriched_only=true` — those are HMM-gated and authored prose
> is `hmm_enriched=false`, so they filter out exactly the layer you want. **GO-DEEP** (`top_k>=25`,
> 3–6 phrasings unioned, expand hits — a few snippets is a failed query). **CANNOT-LIE** (ISMA is
> prose/framing depth, never a metric source — cross-check every number against the tech-baselines
> index).

## Evidence, reproducible

| Route | Same query | Verdict |
|---|---|---|
| `POST /search` | 0.650 | canonical |
| `POST /v2/search` | 0.056, raw transcript blobs | **deprecated → 410** |
| `POST /v2/search/adaptive` | 0.650–0.700, same top doc as canonical | **supported** |

Decisive: a document authored today — verified by direct GraphQL to have **zero tiles** in
`ISMA_Quantum_v2` — is returned by adaptive at **rank 0**. A route that finds a document absent
from the shadow is not querying the shadow.

Reproduce: `curl -s -X POST localhost:8095/v2/search/adaptive -H 'Content-Type: application/json' -d '{"query":"<q>","top_k":5}'`

## The generalisable lesson

**A correction is not landed until it reaches the text that is loaded on every prompt.** Merged
PRs, notifications and even ingested ISMA documents all lose to a standing rule in an
always-loaded file. When retracting a rule, the retraction has to go where the rule lives.
