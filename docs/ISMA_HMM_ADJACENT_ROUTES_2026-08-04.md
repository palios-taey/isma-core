# ISMA HMM-Adjacent Route Measurement - 2026-08-04

## Purpose

Retire the open unmeasured claim in `ISMA_PROSE_RETRIEVAL_SPEC.md` for HMM-adjacent retrieval routes:

- `/v2/search/hmm`
- `/search/motif`
- MCP `isma_motif_search`
- `enriched_only=true`

The policy question is whether any of these routes can be cleared for ISMA prose-depth retrieval. They cannot, based on this measurement. Rule 1 remains unchanged: use `/search` or MCP `isma_search` with `enriched_only=false` for prose-depth retrieval.

## Method

Observed:

- Harness: `isma/scripts/measure_hmm_adjacent_routes.py`
- Production API: `http://localhost:8095`
- Class queried by canonical baseline: `ISMA_Quantum`
- Workload: 6 CPT/packing/sequence-length phrasings, `top_k=30`
- Scale sweep where a route accepts a natural-language query: `full_4096` plus `search_512`
- Comparison unit: `content_hash` sets, not counts
- Field-presence checks: `content`, `source_file`, and `hmm_enriched`
- Durable aggregate artifact: `docs/ISMA_HMM_ADJACENT_ROUTES_2026-08-04.results.json`
- Raw runtime artifact: `/tmp/hmm_adjacent_routes_2026-08-04.json` (`sha256:0f81673394e04d913464088b14ca67fef3e38ea0ae7453010a20eb91b10d9cd2`)

Reproduction command:

```bash
WEAVIATE_URL=http://localhost:8088 \
EMBEDDING_URL=http://localhost:8089/v1/embeddings \
NEO4J_URI=bolt://localhost:7689 \
python3 isma/scripts/measure_hmm_adjacent_routes.py \
  --json-out /tmp/hmm_adjacent_routes_2026-08-04.json
```

## Route Results

Observed:

| Route | Distinct hashes | Distinct `.md` prose | Cross-scale `.md` prose | Identical scale pairs | Content fields | Source fields | `hmm_enriched` field present |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HTTP `/search` baseline | 295 | 284 | 338 | 0/6 | 295 | 295 | 43 |
| HTTP `/v2/search/hmm` | 156 | 152 | 170 | 6/6 | 156 | 156 | 21 |
| HTTP `/search` plus `enriched_only=true` | 295 | 284 | 338 | 0/6 | 295 | 295 | 43 |
| HTTP `/search` plus `hmm_enriched=true` | 251 | 224 | 323 | 0/6 | 251 | 251 | 251 |
| HTTP `/search/motif` with derived motif IDs | 90 | 0 | not applicable | not applicable | 0 | 0 | 0 |
| MCP `isma_motif_search` with derived motif IDs | 90 | 0 | not applicable | not applicable | 0 | 0 | 0 |
| MCP `isma_search(enriched_only=false)` | 273 | 184 | 217 | 0/6 | 273 | 273 | 273 |
| MCP `isma_search(enriched_only=true)` | 178 | 94 | 111 | 0/6 | 178 | 178 | 178 |

Observed set comparisons:

- HTTP `/search` vs `/v2/search/hmm`: 0/12 matching calls had identical content-hash sets.
- HTTP `/search` vs HTTP `/search` plus `enriched_only=true`: 12/12 matching calls had identical content-hash sets.
- HTTP `/search` vs HTTP `/search` plus `hmm_enriched=true`: 0/12 matching calls had identical content-hash sets.
- MCP `isma_search(enriched_only=false)` vs MCP `isma_search(enriched_only=true)`: 0/12 matching calls had identical content-hash sets; `enriched_only=true` was a strict per-call subset of default in 12/12 calls.

## Findings

### `/v2/search/hmm`

Observed:

- Returned 156 distinct hashes and 152 distinct `.md` prose hits across the sweep.
- Produced 170 cross-scale `.md` prose hits, compared with 338 for canonical HTTP `/search`.
- Returned identical `full_4096` and `search_512` hash sets in 6/6 query pairs.
- `V2SearchRequest` has no `scale` field, so `scale` in the request payload is accepted by the service but not consumed by the route.

Inferred:

- This route under-covers prose for GO-DEEP because the scale union is a no-op and the route cannot honor the requested depth/precision split.

Unknown:

- The exact contribution of V2 adaptive ranking versus scale omission to every missing baseline hit was not isolated.

### HTTP `/search` with `enriched_only=true`

Observed:

- Returned exactly the same content-hash sets as default HTTP `/search` in 12/12 matching calls.
- Returned the same union counts as default HTTP `/search`: 295 distinct hashes and 284 distinct `.md` prose hits.
- `SearchRequest` has `hmm_enriched`, not `enriched_only`.
- Passing the correct HTTP key, `hmm_enriched=true`, changed the result set and returned `hmm_enriched` on 251/251 distinct hashes.

Inferred:

- HTTP `/search` plus `enriched_only=true` is a false-success no-op, not an HMM-enriched prose route.

Unknown:

- For default HTTP `/search`, enrichment composition remains unknown when the response omits the `hmm_enriched` field; only 43/295 distinct baseline hashes exposed that field in this sweep.

### MCP `isma_search(enriched_only=true)`

Observed:

- Default MCP `isma_search` returned 273 distinct hashes and 184 distinct `.md` prose hits.
- MCP `isma_search(enriched_only=true)` returned 178 distinct hashes and 94 distinct `.md` prose hits.
- In 12/12 matching calls, the enriched-only hashes were a subset of the default MCP hashes.
- The MCP handler applies `enriched_only` as a post-filter on returned `TileResult` objects.

Inferred:

- MCP `enriched_only=true` removes unenriched prose after retrieval and does not refill to recover prose-depth coverage.

Unknown:

- The full unenriched prose population that could be recovered by a separate enrichment-aware retrieval strategy was not measured here.

### `/search/motif` and MCP `isma_motif_search`

Observed:

- HTTP `/search/motif` rejects a natural-language query-only payload with HTTP 422 because `motif_id` is required.
- `MotifSearchRequest` requires `motif_id` and has no `query` or `scale` field.
- MCP `isma_motif_search` requires `motif_id` and has no `query` or `scale` field.
- Motif assignment on the six phrasings produced derived motif IDs for 5/6 phrasings; one phrasing produced no motif.
- The five derived-motif HTTP `/search/motif` calls returned 90 distinct tile hashes with 0 `content` fields and 0 `source_file` fields.
- MCP `isma_motif_search` returned the same 90 distinct tile hashes with 0 `content` fields and 0 `source_file` fields.

Inferred:

- Motif routes are motif-ID summary lookups, not natural-language prose-depth retrieval routes.

Unknown:

- The source-file provenance of those motif tile hashes was not resolved out-of-band through graph or tile expansion.

## Conclusion

Observed:

- No measured HMM-adjacent route is suitable as a GO-DEEP prose-depth route.
- `/v2/search/hmm` under-covers because scale selection collapses.
- HTTP `/search` plus `enriched_only=true` is a no-op caused by the wrong HTTP request field.
- MCP `isma_search(enriched_only=true)` actively filters and under-serves authored prose.
- Motif routes require motif IDs and return summary-shaped tile hashes rather than prose content.

Inferred:

- The existing Rule 1 routing policy is correct: avoid HMM, motif, and enriched-only paths for prose-depth retrieval.

Unknown:

- The exact source-file provenance for motif tile hashes and the exact V2 ranking contribution remain unresolved as noted above; neither unresolved point clears a route for prose-depth retrieval.
