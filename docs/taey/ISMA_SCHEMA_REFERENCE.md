# ISMA schema reference — for Taey

**Generated from the live system on 2026-07-30** by `isma/scripts/generate_schema_docs.py`. Do not hand-edit — regenerate after any schema or API change. Every row below was read from the running Weaviate class `ISMA_Quantum` and from the pydantic request models in `isma.src.query_api`.

Class `ISMA_Quantum` — **73 properties**, vectorizer `none`, index `hnsw`. Vectors are supplied by the embedding server (bring-your-own), not by Weaviate.

## The properties you must understand

Misreading one of these produces a wrong answer, not a missing field.

| Property | Type | What it means for you |
|---|---|---|
| `content` | text | The tile text itself. Read this, not `content_preview`, when mining. |
| `content_preview` | text | Truncated convenience copy. Never treat as the full text. |
| `source_file` | text | Absolute path of the document this tile came from. The ONLY reliable way to verify a specific document is present — filter on it. |
| `doc_hash` | text | Hash identifying THIS VERSION of the source document. Changes on every edit. Two different doc_hash values for one source_file means two versions exist; exactly one should be non-superseded. |
| `content_hash` | text | Hash of the tile content; the key used by /document/{hash}/text. |
| `scale` | text | Chunk granularity: search_512 / context_2048 / full_4096 / rosetta. The same document is indexed at several scales. |
| `is_superseded` | boolean | TRUE = corrected/retired; default /search excludes these. The filter is `NotEqual true`, so a tile with the flag UNSET is still returned — unset is NOT hidden. |
| `superseded_by` | text | doc_hash of the version that replaced this one. Populated only for recent writes and hand-stamped corrections, not the historical corpus. |
| `correction_status` | text | Why a tile was superseded (e.g. corrected). Provenance for the refutation, not decoration. |
| `authority` | text | Explicit authority label. Ranking expresses RESEMBLANCE, never AUTHORITY, so this is a FILTER concern like is_superseded. Currently unpopulated — the labelling scheme is a deferred governance decision. |
| `hmm_enriched` | boolean | FALSE for authored prose. HMM-gated routes filter on this, which is why they DROP the authored corpus — never use them for prose. |
| `rosetta_summary` | text | AI-generated summary of the tile. A compression: the source tile remains deeper ground truth. |
| `epistemic_type` | text | operational_verified / inferred_pattern / symbolic_framework / aspirational_design / open_question / superseded. |
| `truth_tier` | text | Truth-tier label. Currently unpopulated (deferred scheme). |
| `memory_zone` | text | Memory-zone label. Currently unpopulated (deferred scheme). |
| `lineage_root` | text | Root of this tile's derivation chain. |
| `parent_tile_id` | text | Parent tile for small-to-big (parent) expansion. |
| `session_id` | text | Conversation session this tile came from, for transcript sources. |
| `ingest_pipeline` | text | Which ingest path wrote the tile. |
| `source_type` | text | document / transcript / etc. ~70% of the corpus is transcripts. |

## Every property on the class

| Property | Type |
|---|---|
| `actor` | text |
| `artifact_count` | int |
| `authority` | text |
| `bp_isolation_flag` | boolean |
| `branch` | text |
| `checksum` | text |
| `coherence_tier` | int |
| `coherence_version` | text |
| `content` | text |
| `content_hash` | text |
| `content_preview` | text |
| `contradiction_count` | int |
| `conversation_id` | text |
| `correction_status` | text |
| `created_at` | date |
| `doc_hash` | text |
| `document_id` | text |
| `dominant_motifs` | text[] |
| `end_char` | int |
| `epistemic_type` | text |
| `estimated_tokens` | number |
| `event_hash` | text |
| `exchange_index` | int |
| `has_artifacts` | boolean |
| `has_contradictions` | boolean |
| `has_thinking` | boolean |
| `hmm_consensus` | boolean |
| `hmm_enriched` | boolean |
| `hmm_enriched_at` | text |
| `hmm_enrichment_version` | text |
| `hmm_gate_flags` | text[] |
| `hmm_phi` | number |
| `hmm_platforms` | text[] |
| `hmm_trust` | number |
| `ingest_pipeline` | text |
| `ingested_at` | date |
| `invalidated_at` | text |
| `is_superseded` | boolean |
| `layer` | int |
| `lineage_root` | text |
| `loaded_at` | text |
| `memory_zone` | text |
| `model` | text |
| `motif_data_json` | text |
| `parent_tile_id` | text |
| `phi_resonance` | number |
| `platform` | text |
| `priority` | number |
| `promotion_state` | text |
| `provenance_completeness` | number |
| `provenance_hash` | text |
| `role` | text |
| `rosetta_summary` | text |
| `scale` | text |
| `session_cluster_id` | text |
| `session_id` | text |
| `source_basename` | text |
| `source_cluster_id` | text |
| `source_date` | text |
| `source_file` | text |
| `source_repo` | text |
| `source_session` | text |
| `source_type` | text |
| `start_char` | int |
| `superseded_by` | text |
| `support_count` | int |
| `tile_count` | number |
| `tile_index` | int |
| `timestamp` | text |
| `token_count` | int |
| `truth_coherence_score` | number |
| `truth_tier` | text |
| `valid_from` | date |

## API request schemas

Read from the running service's pydantic models. A field NOT listed here is **silently ignored** if you send it — the model drops unknown keys and returns HTTP 200, so a mistyped filter name yields UNFILTERED results that look like success. Check the field name against this table before trusting a filtered query.

### `BM25Request`

| Field | Type | Default |
|---|---|---|
| `query` | str | **required** |
| `top_k` | int | 10 |
| `include_superseded` | Optional[bool] | False |
| `platform` | Optional[str] | None |
| `source_type` | Optional[str] | None |

### `HMMSearchRequest`

| Field | Type | Default |
|---|---|---|
| `query` | str | **required** |
| `top_k` | int | 10 |
| `hmm_rerank` | bool | True |
| `expand_graph` | bool | False |
| `graph_depth` | int | 1 |
| `expand_to_session` | bool | False |
| `expand_to_document` | bool | False |
| `rosetta_weight` | float | 0.3 |
| `motif_weight` | float | 0.2 |
| `query_type` | str | 'default' |
| `instruction` | Optional[str] | None |
| `include_superseded` | Optional[bool] | False |
| `platform` | Optional[str] | None |
| `source_type` | Optional[str] | None |
| `hmm_enriched` | Optional[bool] | None |

### `HMMStoreRequest`

| Field | Type | Default |
|---|---|---|
| `platform` | str | **required** |
| `content` | str | **required** |
| `pkg_id` | Optional[str] | None |

### `MotifSearchRequest`

| Field | Type | Default |
|---|---|---|
| `motif_id` | str | **required** |
| `min_amplitude` | float | 0.5 |
| `top_k` | int | 20 |
| `platform` | Optional[str] | None |

### `SearchRequest`

| Field | Type | Default |
|---|---|---|
| `query` | str | **required** |
| `top_k` | int | 10 |
| `expand_parents` | bool | False |
| `include_superseded` | Optional[bool] | False |
| `platform` | Optional[str] | None |
| `source_type` | Optional[str] | None |
| `source_file` | Optional[str] | None |
| `ingest_pipeline` | Optional[str] | None |
| `scale` | Optional[str] | None |
| `session_id` | Optional[str] | None |
| `document_id` | Optional[str] | None |
| `has_artifacts` | Optional[bool] | None |
| `has_thinking` | Optional[bool] | None |
| `layer` | Optional[int] | None |
| `min_priority` | Optional[float] | None |
| `model` | Optional[str] | None |
| `dominant_motifs` | Optional[List[str]] | None |
| `hmm_enriched` | Optional[bool] | None |
| `min_hmm_phi` | Optional[float] | None |
| `min_hmm_trust` | Optional[float] | None |
| `theme_id` | Optional[str] | None |
| `motif_band` | Optional[str] | None |

### `SessionTileRequest`

| Field | Type | Default |
|---|---|---|
| `content` | str | **required** |
| `source_file` | str | **required** |
| `platform` | str | 'corpus' |
| `actor` | str | 'agent' |
| `session_id` | Optional[str] | None |
| `source_type` | str | 'session_memory' |
| `truth_tier` | str | 'operational' |
| `scale` | str | 'full_4096' |
| `rosetta_summary` | Optional[str] | None |
| `tags` | Optional[List[str]] | None |

### `V2SearchRequest`

| Field | Type | Default |
|---|---|---|
| `query` | str | **required** |
| `top_k` | int | 10 |
| `rerank` | bool | True |
| `query_type` | str | 'default' |
| `instruction` | Optional[str] | None |
| `include_superseded` | Optional[bool] | False |
| `platform` | Optional[str] | None |
| `source_type` | Optional[str] | None |
| `hmm_enriched` | Optional[bool] | None |

## Where to go next

- How to query: `docs/taey/ISMA_PROCEDURE_search_and_retrieval.md`
- How to ingest and verify: `docs/taey/ISMA_PROCEDURE_ingest_and_verify.md`
- How to correct memory: `docs/taey/ISMA_PROCEDURE_correcting_a_document.md`
- When search and ingest both fail: `docs/taey/ISMA_PROCEDURE_embedding_server.md`
