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
