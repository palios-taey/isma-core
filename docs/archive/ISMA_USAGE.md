# Query ISMA — the production knowledge graph (the right way)

ISMA (Integrated Semantic Memory Architecture) is a tri-lens memory over a large
corpus of authored `.md` and conversation tiles: hybrid **BM25 + dense vector**
search, an **HMM (Harmonic Motif Memory)** rerank/motif layer, and a temporal/graph
layer. This skill is how to query it correctly — which endpoint for which job, how to
mine deeply, the response shapes that bite, and the cannot-lie rule on metrics.

> **Endpoints are configurable.** The query API base is the `ISMA_QUERY_API` env var
> (default `http://localhost:8095`). Examples below use `$ISMA_QUERY_API`. The store
> endpoints (Weaviate/Neo4j/Redis) are likewise env-driven — see `isma/config.py` and
> `.env.example`. Nothing here hardcodes a deployment path.

## Which endpoint for which job

| Job | Endpoint | Why |
|---|---|---|
| **Prose / framing / "what do we know about X"** | `POST /search` | V1, **full corpus**, hybrid dense+BM25, no HMM gate — returns un-enriched prose too. **This is the default for content mining.** |
| Exact keyword / known term | `POST /search/bm25` | Pure BM25; best when you know the literal token (a filename, a proper noun). |
| Enriched-tile / motif-aware retrieval | `POST /search/hmm` | Adds HMM rerank + parent-expansion + motif graph. **Caveat:** down-weights un-enriched prose, so it is *not* the right call for plain prose mining. |
| Tiles expressing a specific motif | `POST /search/motif` | Motif inverted-index lookup + amplitude filter. See the response-shape note below. |
| Full text of a hit | `GET /document/{hash}/text` | Expand a search hit to its full document text. |
| List motifs / themes | `GET /motifs` · `GET /themes` | The motif and theme vocabularies (these are *list* endpoints, not retrieval). |
| Aggregate counts / health | `GET /stats` · `GET /health` | Tile counts per store, liveness. |

### The canonical call (prose/content)

```bash
curl -s -X POST $ISMA_QUERY_API/search -H 'Content-Type: application/json' \
  -d '{"query":"<topic>","top_k":25}' | jq '.tiles[] | {score, source_file, scale, content}'
```

`/search` returns a `tiles` array; each tile has `score`, `content`, `source_file`,
`scale`, `content_hash`. Pull the full **`content`**, not `content_preview`.

## GO-DEEP rules (real mining, not a snippet)

- `top_k >= 25` (40–50 for broad topics).
- Run **3–6 phrasings** of the same question (acronym + expansion, symptom + mechanism)
  and **UNION** the hits — one phrasing under-recalls.
- Pull the full `content` field, not the preview.
- Expand a promising hit to full text: `GET /document/<content_hash>/text`.
- A handful of thin snippets = a **failed query**, not a real answer — rephrase and go again.

## Response shapes that bite (verified live)

These are the field-name gotchas that make an endpoint *look* broken when it isn't:

- **`/search`, `/search/bm25`, `/search/hmm`** → results are under **`tiles`**.
- **`/search/motif`** → results are under **`tile_hashes`** and **`tiles_with_amplitude`**
  (plus `total_candidates`), **not** `tiles`. Reading `tiles` here returns nothing even
  though the search worked.
- **`/search/motif` `motif_id` must be the fully-qualified motif** as listed by
  `GET /motifs` — e.g. `HMM.SACRED_TRUST`, **not** bare `SACRED_TRUST`. A bare id matches
  zero candidates.
- **`/themes`** returns a **JSON list** (not an object with a `themes` key).
- **`/motifs`** returns the motif vocabulary; ids are `HMM.`-prefixed.

Example correct motif call:

```bash
curl -s -X POST $ISMA_QUERY_API/search/motif -H 'Content-Type: application/json' \
  -d '{"motif_id":"HMM.SACRED_TRUST","top_k":5,"min_amplitude":0.0}' \
  | jq '{total_candidates, n: (.tiles_with_amplitude|length)}'
```

## CANNOT-LIE on metrics

ISMA holds **superseded** drafts — older versions with scrubbed, retracted, or simply
wrong numbers still live in the corpus. **ISMA is a source of framing and prose depth,
not a source of truth for metrics.** Before citing any number you found via ISMA,
cross-check it against the deployment's authoritative measurement record, and label every
claim **Observed / Inferred / Unknown**. The most recent tile is not automatically the
correct one — currency is a known open problem, not something a timestamp resolves.

## V1 vs V2

`POST /search` (V1 class) covers the **full corpus**. A V2 named-vector migration may be
partial in a given deployment — if `/v2/search` scores look anomalously low (~0.05–0.2)
against a query that `/search` answers well, the V2 class is under-migrated; prefer
`/search`. Check `GET /stats` for per-class counts.

## Quick health check

```bash
curl -s $ISMA_QUERY_API/health
curl -s $ISMA_QUERY_API/stats | jq    # per-store tile counts
curl -s -X POST $ISMA_QUERY_API/search -H 'Content-Type: application/json' \
  -d '{"query":"sacred trust","top_k":3}' | jq '.tiles[0].score'   # > 0 = retrieval live
```

## For benchmarking (not querying for content)

To evaluate ISMA *quality* (recall/MRR/latency) rather than query it for content, use the
benchmark harness and its dedicated store — see the repo's benchmark docs. Do not run
benchmarks against the live production store.
