# CLAUDE.md

This repository is the public ISMA core: retrieval, ingest, query API, HMM storage hooks, demo assets, and the local embedding server wrapper. Treat it as a reusable product repo, not an operator-specific workspace.

## Architecture

- `isma/src/` contains the importable package.
- `isma/src/query_api.py` exposes the FastAPI query surface.
- `isma/src/retrieval.py` and `isma/src/retrieval_v2.py` implement the retrieval pipelines.
- `isma/src/hmm/` contains motif and storage helpers used by enrichment flows.
- `isma/scripts/` contains operational CLIs for ingest, benchmarking, packaging, and backfills.
- `server.py` is the embedding server entrypoint.
- `demo/` contains the demo corpus and setup script.

## Configuration

- Core dependencies are environment-driven.
- `WEAVIATE_URL` and `EMBEDDING_URL` fail loud when unset.
- `NEO4J_URI` defaults to localhost unless you override it.
- Production `WEAVIATE_URL` is `http://localhost:8088`; the local `docker compose` demo still maps the store on `8080`.
- Copy `.env.example` to `.env` and fill in your own service endpoints before running anything substantial.
- Do not hardcode machine-local paths or private network addresses in committed code.

## Development

- Start the local demo stack with `docker compose up -d`.
- Start the embedding server with `bash ./start-local.sh` or your own `python3 server.py` wrapper.
- Run the query API with `uvicorn isma.src.query_api:app --host 0.0.0.0 --port 8095`.
- Run the demo ingest/query flow with `python3 demo/setup_demo.py`.
- Benchmark retrieval with `python3 isma/scripts/benchmark_retrieval.py --label your_run`.

## Ingest

- `isma/scripts/ingest_md_file.py` ingests one markdown file into Weaviate after phi-tiling.
- `isma/scripts/backfill_md_corpus.py` requires a newline-delimited roots file passed via `--roots-file` or `ISMA_MD_ROOTS_FILE`.
- `isma/scripts/watch_md_corpus.sh` is the periodic watcher wrapper; it fails loud unless `ISMA_MD_ROOTS_FILE` is set.

## Code Discipline

- Keep imports and runtime paths repo-relative.
- Prefer fail-loud behavior over silent fallbacks for missing infrastructure or malformed data.
- Preserve reproducibility: benchmark claims and public metrics must map back to committed artifacts.
- Before changing shared functions, run impact analysis with your code-intelligence tool and verify the blast radius.
- Keep commits narrow and reviewable. Do not mix packaging, behavior changes, and product-copy rewrites unless the task explicitly requires it.

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
