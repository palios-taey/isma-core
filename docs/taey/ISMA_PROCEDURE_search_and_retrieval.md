# How to search ISMA for what we know about a topic — the correct ISMA query procedure

**Owner:** weaver · **Canonical as of 2026-07-30** · Full spec: `isma-core/ISMA_PROSE_RETRIEVAL_SPEC.md`.
This is the procedure for asking ISMA *"what do we know / what have we said about X"*.

## Which ISMA endpoint to use for searching the knowledge graph

Use the **V1 search endpoint**, which queries the full corpus (`ISMA_Quantum`, ~1.6M tiles):

```bash
curl -s -X POST http://localhost:8095/search \
  -H 'Content-Type: application/json' -d '{"query":"<full-sentence question>","top_k":25}'
```

Or the CLI, which has the correct defaults already baked in: `isma-query "<full-sentence question>"`.
Taey's own tool surface is `search_isma` with `search_type=semantic`. For exact-term / keyword lookup use
`POST http://localhost:8095/search/bm25`. To expand a hit into its full document text:
`GET http://localhost:8095/document/<content_hash>/text`.

## Which ISMA endpoints you must NEVER use for prose

- **Never `/v2/search` or any `/v2/*` route.** `ISMA_Quantum_v2` is a ~4.6% partial migration (73,809 tiles
  vs 1,605,584) that receives no new writes and contains **zero** tiles of recently authored documents.
  Measured: the same query scored **0.650 on V1 vs 0.072 on v2**. A v2 answer looks plausible while being
  built on a fraction of what we know — a near-miss that resembles success, which is the worst failure mode.
- **Never `isma_adaptive_search`** — it routes to that same v2 shadow.
- **Never `/search/hmm` or `/search/motif` for prose**, and **never `enriched_only=true`.** These are
  HMM-gated, and the authored `.md` prose is `hmm_enriched=false`, so the gate filters out exactly the
  high-value authored layer you are trying to read.
- **Never hand-roll a raw Weaviate query** that drops the default `is_superseded` filter — you would
  retrieve corrected, superseded drafts as if they were current.

## GO-DEEP — how to query ISMA properly instead of taking the first snippet

A single query returning two or three thin snippets is a **failed query**, not an answer. Retrieval from
ISMA is brittle across phrasings, so depth comes from unioning several:

- Use `top_k` of **at least 25** (40–50 for a broad topic).
- Run **3–6 different phrasings** of the question — acronym and expansion, symptom and mechanism — and
  **union** the results. Measured 2026-07-30: a document that is the single best answer to a question still
  only surfaced on **4 of 6** natural phrasings of that same question. One phrasing is a coin flip; the union
  is the answer.
- Prefer **full-sentence questions** over noun-bags — measured to beat noun-bag retrieval on 3 of 3 topics.
- Pull the full `content` field, not `content_preview`, and expand promising hits to full document text.
- Success looks like: the union across phrasings visibly shapes what you write next. Two thin snippets
  means rephrase and go again — do not proceed on it.

## ISMA is a prose source, never a metric source

**Cannot-lie rule:** ISMA holds superseded drafts, retracted numbers, and aspirational design text. Use it
for framing, history, wording, and depth — **never as the source of a number you are about to publish.**
Cross-check every figure against `treasurer/foundations/tech_baselines/INDEX.md` and label each claim
Observed / Inferred / Unknown.

## Why ISMA cannot give you THE one authoritative value for a key

`/search` is a **similarity ranker**: it returns what is most *like* your question, which is not the same as
the one right answer. Measured twice — asked for its own canonical retrieval endpoint (a fact with exactly
one correct answer whose source doc is indexed), it returned unrelated packets and the authoritative spec did
not appear. There is also no stable get-by-key surface: every ID-addressable route is keyed by
`content_hash`, which **changes on every edit** — backwards from what canonical lookup needs. For "what is
the current value of X", use the KB. For "what do we know about X", use ISMA. Do not force either onto the
other's substrate.

## If ISMA search fails

Notify weaver, and check `isma-core/ISMA_PROSE_RETRIEVAL_SPEC.md` first — it decides the fork. A wrong
endpoint, a dead service, or a v2/HMM route is a **BUG** that weaver fixes. A correct route with a noun-bag
query, a single phrasing, or a thin union is a **query-technique gap**, not an ISMA failure.
