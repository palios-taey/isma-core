# AGENTS.md — using ISMA-core from an AI agent

Guide for AI coding agents / assistants (Claude Code, Cursor, other MCP clients) and autonomous
adopters integrating ISMA-core as a retrieval backend. For repo architecture see `CLAUDE.md`; for
rough edges see `KNOWN_FINDINGS.md` (Known Limitations).

## Two ways to use it

### 1. MCP server (for MCP-capable agents)

`isma/src/mcp_server.py` exposes ISMA retrieval as MCP tools over stdio. Set `WEAVIATE_URL`,
`EMBEDDING_URL`, and optionally `NEO4J_URI` / `REDIS_HOST` first (see `.env.example`), then point your
MCP client at the server.

| Tool | Purpose |
|------|---------|
| `isma_search` | Hybrid vector + BM25 semantic search (top-k); optional `platform` / `scale` filters |
| `isma_adaptive_search` | Auto-classifies the query (exact / temporal / conceptual / relational / motif) and routes to the best strategy |
| `isma_motif_search` | Tiles expressing a given motif, ranked by amplitude |
| `isma_get_tile` | Full content + metadata for a `content_hash` (all scales) |
| `isma_graph_traverse` | Follow Neo4j `RELATES_TO` / `EXPRESSES` edges from a tile |
| `isma_stats` | Index statistics |
| `isma_cypher` | Raw Cypher against the graph — **advisory read-only** (see Limitations) |

### 2. HTTP query API (any agent / language)

```bash
uvicorn isma.src.query_api:app --host 0.0.0.0 --port 8095
```

Endpoints include `/search`, `/search/hmm`, `/search/bm25`, `/search/motif`, `/stats`, `/health`,
`/document/...`. Read/search endpoints are open; write endpoints require `ISMA_API_KEY` (header
`X-API-Key`).

```bash
curl -X POST localhost:8095/search -H 'Content-Type: application/json' \
  -d '{"query": "your question", "top_k": 10}'
```

## Setup an adopter must satisfy

1. `cp .env.example .env` — set `WEAVIATE_URL` + `EMBEDDING_URL` (required; they fail loud if unset).
   `NEO4J_URI` is optional (only the graph-enrichment features use it).
2. `pip install .` for the core, or `pip install .[server]` to also run the bundled
   Qwen3-Embedding-8B server. (`requirements.txt` is the equivalent for a non-packaged checkout.)
3. **Bring your own embedding endpoint** (any OpenAI-compatible `/embed` endpoint via `EMBEDDING_URL`),
   or run the bundled one with `./start.sh`.
4. Ingest a corpus: `python3 demo/setup_demo.py` (drop `.md` files in `demo/corpus/` first) for a quick
   demo, or your own ingestion pipeline for production. Retrieval quality scales with your own corpus —
   the published benchmarks (BEIR SciFact) measure the generalizable core, not a bundled corpus.

## Limitations agents should know (full list in `KNOWN_FINDINGS.md`)

- `isma_cypher` is **advisory** read-only — Neo4j Community `READ_ACCESS` is a routing hint, not a
  write-block. Do not expose it to untrusted callers expecting a hard guarantee.
- `isma_graph_traverse` caps `depth` at 3 (declared in the tool's JSON schema).
- Hybrid BEIR recall varies ~0.3% run-to-run; dense retrieval is exactly reproducible.
- **Memory governance (validity / supersede):** re-ingesting a newer version of a doc marks the
  prior tiles superseded; superseded tiles are **excluded from retrieval by default** (pass
  `include_superseded=true` on `search` / `/tiles` to include them). Policy + fields in
  `MEMORY_GOVERNANCE.md`; dry-run audit of eviction candidates via
  `python3 -m isma.scripts.decay_sweep`. Enabling this on an **existing** store requires the
  `is_superseded` property to be **present in the schema** (a fresh store auto-creates it on first
  write); a values-backfill is optional — the filter matches un-flagged tiles, so legacy tiles stay
  visible (only `is_superseded=true` is excluded).
