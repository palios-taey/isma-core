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
