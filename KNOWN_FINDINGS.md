# Known Limitations & Reproducibility Notes

Honest notes for adopters: what reproduces exactly, what varies, and the rough edges in the
optional advanced surface. Nothing here blocks the core retrieval path.

## Benchmark reproducibility (BEIR SciFact)

- **Dense retrieval reproduces exactly** — `Recall@10 0.800 / nDCG@10 0.651 / MRR@10 0.608`, identical
  across independent runs (the embedding vectors are deterministic for identical inputs).
- **Hybrid (dense + BM25) varies ~0.3% run-to-run** — observed `Recall@10` in the range **0.865–0.869**
  across independent end-to-end runs (committed artifact `benchmarks/results/scifact.json` holds the
  upper run). The variance is in the BM25/fusion component (tie-breaking / index-state sensitivity); the
  dense path itself is bit-stable. Treat hybrid as a range, not a single fixed point.
- These numbers reflect the **generalizable retrieval core** (embedding model + Weaviate hybrid search)
  with the optional enrichment layers (rerank / phi-tiling / query classifier) **off** — no
  corpus-specific tuning. Reproduce with `BEIR_DATASET=scifact python3 benchmarks/beir_eval.py`.

## Memory governance (validity / supersede)

- **Supersede-exclusion needs the `is_superseded` property present in the schema on an existing store.**
  Re-ingesting a newer version flags prior tiles `is_superseded=true`; retrieval default-excludes them
  via a boolean filter (`{is_superseded NotEqual true}` — a boolean, not an empty-string text filter,
  which Weaviate rejects on a word-tokenized field). A **fresh** store auto-creates the property on the
  first governed write. An **existing** store must have the property **present in the `ISMA_Quantum`
  schema** before the filter serves (else queries error `"no such prop"`), AND **a one-time
  backfill** (`is_superseded=false` on existing tiles) is **required on a populated store** to
  materialize the property's inverted-index bucket — otherwise the filter errors `"bucket for prop
  is_superseded not found - is it indexed?"` (verified on the live 1.5M-tile store: adding the
  property is not enough; the bucket only exists once values are written). A **fresh** store
  auto-materializes on first governed write. Once the bucket exists, `NotEqual true` also matches any
  still-unflagged tiles (graceful), so a partial backfill degrades safely — but at least
  bucket-materialization is mandatory. Policy + fields: `MEMORY_GOVERNANCE.md`; evidence: `audit_logs/p4_production_evidence.md`.
- History/timeline reconstruction intentionally still sees superseded tiles (the exclusion is for
  answering queries, not for auditing lineage).

### Scope of supersede-on-write (what it does and does NOT cover)

Read-side exclusion of `is_superseded=true` tiles is applied on the **primary answering/search paths**:
the V1 and V2 query-builders, the `ISMACore._semantic_search`/`recall` hybrid/BM25/vector queries, and
the V2 overlap-context fetch. It is **NOT yet applied on several secondary paths** (an independent audit
found these) — a superseded tile can still surface through them:
- parent/context expansion by id (`ISMACore._fetch_tile_by_id`, `retrieval._get_tile_by_id` via
  `_expand_parents`) — direct UUID GETs with no validity check;
- V2 content backfill (`retrieval_v2._fill_content`) — fetches by `content_hash` only;
- relational adaptive lanes (`relational_retrieval` State Beta / State Gamma near-vector);
- the MCP `isma_get_tile` tool — returns a tile by hash/scale unfiltered.
Filtering these is tracked follow-up work; until then, exclusion is **primary-path**, not absolute.

Supersede-*on-write* is also **scoped** — adopters should know its boundaries:

- **Trigger is hash/lineage match, not arbitrary content change.** `_embed_to_weaviate` finds priors by
  matching `content_hash` or an explicit `lineage_root`. A *changed* document only auto-supersedes its
  prior version if the caller supplies the prior `lineage_root`; otherwise the new content gets a new
  hash and coexists with the old. Re-ingesting *identical* content supersedes correctly.
- **Only the primary ingest path supersedes.** `ISMACore._embed_to_weaviate` does the find+invalidate.
  Other writers stamp `is_superseded=false` but do **not** supersede priors: the `/ingest/session` API,
  and they are un-stamped entirely in `scripts/hmm_store_results.py` and `scripts/ingest_md_file.py`
  (their tiles are still *retrievable* — `NotEqual true` matches unflagged tiles — but not governed).
- **Prior-tile invalidation is capped at 50 per write** (no pagination); a lineage with >50 prior
  versions may retain some unflagged.
- **V2 canonical class (`ISMA_Quantum_v2`) is not propagated to** by the supersede writer, which patches
  `ISMA_Quantum`; ensure the property exists in both classes you query.
- `scripts/backfill_md_corpus.py --purge-on-change` hard-deletes stale `watch_md_v1` tiles (that path is
  delete-not-supersede by design).

These are honest scope boundaries. The **primary-path** read-side exclusion + the primary-path
supersede-on-write are implemented and independently reviewed; **complete** exclusion across every
secondary read path and a fully atomic/dead-lettered queue path are tracked follow-up work, not yet
claimed as done.

## Configuration behavior

- **Neo4j is optional** — only the graph-enrichment features use it; core search imports and runs
  without it (`NEO4J_URI` defaults to localhost).
- **`WEAVIATE_URL` and `EMBEDDING_URL` are required** and fail loud if unset (so you never silently
  connect to the wrong backend). The documented quick-start covers this: `cp .env.example .env` first,
  and `pip install -r requirements.txt` (includes `python-dotenv` so `.env` loads).
- **State directory** defaults to `~/.local/share/isma` (user-writable; no root privileges needed).
  Override `ISMA_STATE_DIR` for a system-wide deployment.

## Known rough edges (optional advanced MCP/HMM surface — not the core RAG path)

- **`isma_cypher` is advisory read-only, not enforced.** It uses Neo4j's `READ_ACCESS` session mode,
  which is a routing hint, not a write-block, on single-node (non-cluster) Neo4j Community — a
  sufficiently crafted side-effectful Cypher could still mutate. Treat the tool as advisory; do not
  expose it to untrusted callers expecting a hard read-only guarantee.
- **`isma_graph_traverse` depth is capped at 3.** This is now declared in the tool's JSON schema
  (`"maximum": 3`), so clients see the constraint rather than hitting it silently.

(Previously listed here and since fixed: the `isma_search` empty-scale-filter result now returns an
explanatory `note`; `hmm_package_builder` ownership now uses an exact match, not a substring.)

## License

MIT — see `LICENSE`.
