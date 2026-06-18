# ISMA Prose-Depth Retrieval Spec - NO-HMM, GO-DEEP

**Status:** reference workflow. **For:** any ISMA deployment that needs deep prose retrieval over authored markdown and corpus content.

**Why this exists:** ~2,400 of our authored `.md` files (foundations, recaps, drafts, docs, corpus) are now hybrid-searchable in ISMA as PROSE. Use this for "what do we *know/say* about X" depth when drafting posts, briefs, replies, analyses. It complements GitNexus (code intel) — this is the prose/framing layer.

---

## RULE 1 — NO HMM. (Critical: HMM gating silently hides the new prose.)

The newly-ingested prose carries `hmm_enriched=false`. Any HMM-gated path EXCLUDES it.

- ✅ USE: `/v2/search`, `/v2/search/adaptive`, `/search` (plain hybrid BM25+vector).
- ✅ MCP: `isma_search` / `isma_adaptive_search` with **`enriched_only=false`** (the default — just don't set it true).
- ❌ NEVER: `/search/hmm`, `/v2/search/hmm`, `/search/motif`, `isma_motif_search`, or `enriched_only=true`. These filter to HMM-enriched tiles and will return a fraction of what exists — or miss the prose entirely.

## RULE 2 — GO DEEP. (No "a few results." Depth is mandatory.)

Defaults return ~10 shallow snippets. That is NOT acceptable for our work. Depth recipe:

1. **High top_k:** `top_k` ≥ 25 (use 40–50 for broad topics). No server cap — it returns what you ask.
2. **Full passages, not snippets:** request **`scale:"full_4096"`** for complete ~2K-char passages. (search_512 is for pinpoint precision; full_4096 is for depth/context.) Do a depth pass at full_4096 AND a precision pass at search_512, then union.
3. **Pull the whole `content` field**, never `content_preview`.
4. **Multi-query:** run 3–6 phrasings of the topic (synonyms, the acronym + the expansion, the symptom + the mechanism) and UNION the hits — single-query retrieval under-covers.
5. **Expand to the full document** when a hit matters: `GET /v2/expand/{content_hash}` or `GET /document/{content_hash}/text` returns the complete source file, not just the tile.
6. **Read across sources:** don't stop at the top hit — synthesize across the deep set. The point is coverage.

## RULE 3 — CANNOT-LIE on metrics.

The prose corpus contains SUPERSEDED drafts with SCRUBBED numbers (e.g. `nvidia_forum_drafts_2026-04-30.md` still has the retracted "22-23 GB/s busbw"). ISMA = prose/framing depth, **NOT a metric source of truth**. Any number you pull MUST be cross-checked against your fleet's canonical-baselines artifact (a maintained per-claim file with source + verifier + verbatim-citation-text columns) before external citation. Label Observed/Inferred/Unknown.

---

## CANONICAL CALLS

### 0. `isma-query` CLI
Wrap the canonical call so users cannot accidentally shallow- or HMM-query:
```bash
isma-query "<topic>"                 # defaults: top_k=25, scale=full_4096, hybrid /v2/search on ISMA_Quantum
isma-query "<topic>" --precision     # search_512 pinpoint pass (union with the default deep pass)
isma-query "<topic>" --our-prose     # narrow to ingest_pipeline=watch_md_v1 (authored prose only)
```
DEFAULT IS UNFILTERED ON PURPOSE - it searches transcripts + corpus + prose together for maximum depth. Only add `--our-prose` when you specifically want authored framing and nothing else. Class is `ISMA_Quantum` via the Query API (NOT a direct Weaviate BM25 shortcut - that is the wrong, shallow path).

### A. HTTP query_api
Base: `http://your-query-host:8095`  (embedding endpoint example: `http://your-embedding-host:8091`; Weaviate example: `http://your-weaviate-host:8088`)

```bash
# DEEP full-passage pass (the workhorse):
curl -s -X POST http://your-query-host:8095/v2/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"<topic phrasing>","top_k":25,"scale":"full_4096"}'

# PRECISION snippet pass (union with the above):
curl -s -X POST http://your-query-host:8095/v2/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"<topic phrasing>","top_k":40,"scale":"search_512"}'

# Scope to OUR authored prose only (optional — drop to include transcripts/corpus too):
#   add  "filters":{"ingest_pipeline":"watch_md_v1"}   (or post-filter client-side on that field)

# Pull a COMPLETE source doc for a hit:
curl -s http://your-query-host:8095/document/<content_hash>/text
curl -s http://your-query-host:8095/v2/expand/<content_hash>
```
Response: `{"tiles":[{content, content_hash, scale, source_file, source_type, hmm_enriched, score, ...}]}`.

### B. MCP
```
isma_adaptive_search(query="what do we know about <topic>", top_k=25)   # entry point, auto-routes
isma_search(query="<phrasing>", top_k=40)                               # hybrid; DO NOT pass enriched_only=true
isma_get_tile(content_hash) / isma_graph_traverse(...)                  # expand/relate
```
NOTE: if your MCP `isma_search(scale="full_4096")` path returns empty, use the **HTTP `/v2/search` scale=full_4096** call above instead of the MCP scale filter.

---

## FIELDS
Class `ISMA_Quantum`. Filter/read: `ingest_pipeline` ("watch_md_v1" = our prose), `source_type` (foundation|recap|audit_packet|document), `source_file`, `source_basename`, `scale` (search_512|context_2048|full_4096), `hmm_enriched`, `content`, `content_preview`, `doc_hash`.

Coverage now: ~2,400 files / 34K search_512 + 8.3K context_2048 + 4.4K full_4096 prose tiles. Auto-updated every 15 min by the `md-corpus-watch` watcher (new/changed `.md` ingested additively).

## ONE-LINE FOR EACH INSTANCE
> Querying ISMA for prose depth? Use `/v2/search` (or `isma_adaptive_search`), `top_k≥25`, do a `scale:"full_4096"` pass + a `search_512` pass, multi-phrase the query, expand the docs that matter, NEVER use an HMM/motif/enriched_only path, and cross-check any number against your fleet's canonical-baselines artifact. Go deep — a handful of tiles is a failed query.
