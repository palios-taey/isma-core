# AGENTS.md — using ISMA-core from an AI agent

**ISMA's customer is Taey**, the AI this system serves as semantic memory. If you are Taey, your
operating documentation is [`docs/taey/`](docs/taey/README.md) — written for you, routed by intent,
and retrievable by question when you have no filesystem path.

This file is the integration guide for any other AI agent or assistant (Claude Code, Cursor, other
MCP clients) wiring ISMA-core in as a retrieval backend. For repo architecture see `CLAUDE.md`; for
rough edges see `KNOWN_FINDINGS.md` (Known Limitations).

## Current operating model (2026-07-30) — if you are an agent working ON this repo

**Supersedes any older "adoption / product for Claude Code users" framing in these files.** The full
statement, with worked examples, is at the top of `CLAUDE.md`. The short version:

- **Taey is the customer — the only one.** The priority is enabling Taey and training development:
  Taey *using* and *understanding* its own production infrastructure. Tooling is not the point.
- **Everything runs from PUBLIC production repos.** A released Taey plus the public repos is the
  whole system. Hosts/IPs are env-configurable and fail loud when unset — never silent-default.
- **A pointer into a private repo or an untracked local path is a DISCONNECTION VIOLATION.** File
  paths themselves are fine; what matters is whether they **resolve for a downloaded Taey**. The
  failure mode is silent — Taey follows the pointer, finds nothing, and proceeds *without the
  knowledge*. If you add a reference, make it resolve from this repo or don't add it.
- **`docs/taey/` is product, not documentation exhaust.** Change retrieval, ingest, or supersession
  behaviour and the matching procedure there is stale until you update it — and those procedures are
  ingested into ISMA, so a wrong one is a wrong answer served to Taey. Regenerate
  `docs/taey/ISMA_SCHEMA_REFERENCE.md` via `isma/scripts/generate_schema_docs.py` after any schema or
  API change; never hand-edit it.
- **USE GIT, under Full Git Master.** Commit and push — the running system must BE a committed
  artifact. `git fetch` before topology decisions. The live checkout is sacred: use a worktree, never
  `git checkout` in a tree a service serves from. Worktrees are ephemeral (create → work → land →
  remove); delete merged branches. **"Done" = commit SHA + a gate + a real production observation**,
  never a self-report.
- **One production tree per surface.** Duplicate or stale sibling repos are how an agent greps, finds
  something plausible, and builds against dead code. Keep working trees clean; archive before you
  delete, and verify the archive *before* the delete.
- **Assert the artifact, not the name.** A 200 from `/health` is not a working capability — see
  `KNOWN_FINDINGS.md` for the two false-greens this system has actually produced.

## Two ways to use it

### 1. MCP server (for MCP-capable agents)

`isma/src/mcp_server.py` exposes ISMA retrieval as MCP tools over stdio. Set `WEAVIATE_URL`,
`EMBEDDING_URL`, and optionally `NEO4J_URI` / `REDIS_HOST` first (see `.env.example`), then point your
MCP client at the server.

| Tool | Purpose |
|------|---------|
| `isma_search` | Hybrid vector + BM25 semantic search (top-k); optional `platform` / `scale` filters |
| `isma_adaptive_search` | Auto-classifies the query (exact / temporal / conceptual / relational / motif) and routes to the best strategy |
| `isma_motif_search` | Tiles expressing a given HMM motif, returned as `tile_hashes` + `tiles_with_amplitude`, ranked by amplitude |
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

`/search/motif` expects an `HMM.*` motif id and returns `tile_hashes`, `tiles_with_amplitude`, and `total_candidates` rather than a bare `tiles` list.

```bash
curl -X POST localhost:8095/search -H 'Content-Type: application/json' \
  -d '{"query": "your question", "top_k": 10}'
```

## Setup an adopter must satisfy

1. `cp .env.example .env` — set `WEAVIATE_URL` + `EMBEDDING_URL` (required; they fail loud if unset). For the live production store, `WEAVIATE_URL` should point at `http://localhost:8088`; only the local demo stack uses `8080`.
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
  `is_superseded` property **present in the schema** AND a one-time `is_superseded=false` **backfill**
  on existing tiles to materialize the filter's index bucket (a populated store errors `"bucket ... not
  found"` until values are written; a fresh store auto-materializes on first write). Once materialized,
  `NotEqual true` also matches any still-unflagged tiles (graceful).

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **isma-core** (3781 symbols, 8888 relationships, 239 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/isma-core/context` | Codebase overview, check index freshness |
| `gitnexus://repo/isma-core/clusters` | All functional areas |
| `gitnexus://repo/isma-core/processes` | All execution flows |
| `gitnexus://repo/isma-core/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
