# ISMA — Hybrid Retrieval (RAG) System

A hybrid retrieval service for document search and RAG: **dense vector + BM25 search**, **multi-scale chunking**, **query-type routing**, optional **cross-encoder reranking**, and a **FastAPI** serving layer. Core retrieval is the primary surface; optional HMM/graph enrichment remains beta. Bring your own embedding model (defaults to Qwen3-Embedding-8B behind an OpenAI-compatible endpoint; any `/embed` endpoint works).

---

## Features

- **Hybrid retrieval** — dense ANN (Weaviate HNSW) fused with BM25 keyword search per query.
- **Multi-scale chunking** — each document is indexed at three granularities (512 / 2048 / 4096 tokens), so retrieval can return a precise snippet *or* the full passage (small-to-big retrieval).
- **Query-type routing** — a lightweight classifier routes exact / conceptual / temporal queries to the strategy that serves them best.
- **Optional cross-encoder reranking** — Qwen3-Reranker-8B. **Disabled by default**: in our testing it did not improve results on our corpus (see [Benchmarks](#benchmarks)); enable and re-benchmark for your data.
- **Filter-aware semantic cache** — Redis-backed, keyed on query + filters.
- **FastAPI service** — `/health`, `/stats`, `/search`, `/search/hmm`, `/search/bm25`, `/search/motif`, `/motifs`, `/themes`, `/document/...`; write endpoints are API-key gated (`X-API-Key`), CORS is explicit (no `*`).
- **Optional graph enrichment** — Neo4j relational metadata for advanced relational retrieval (off the core path; not required to run the system).

---

## Benchmarks

RAG numbers are only meaningful on a **public, reproducible** dataset. We evaluate on **BEIR SciFact** (a standard information-retrieval benchmark with human relevance judgments), so the results are comparable to published baselines.

Evaluated on the **BEIR SciFact** test split (5,183 documents, 300 judged test queries), scored with standard `Recall@k` / `nDCG@k` / `MRR@k` against the published qrels:

| Config | Recall@10 | nDCG@10 | MRR@10 |
|--------|-----------|---------|--------|
| Dense (Qwen3-Embedding-8B) | 0.800 | 0.651 | 0.608 |
| **Hybrid (dense + BM25, α=0.5)** | **0.869** | **0.726** | **0.686** |

*Committed artifact:* `benchmarks/results/scifact.json` holds one published run: hybrid `0.869 / 0.726 / 0.686`. A second observed end-to-end run landed within ~0.3% of that artifact. **Dense reproduces exactly** — identical to four decimals run-to-run. The variance arises in the BM25/fusion component (the dense vector path is bit-stable); *inferred* cause is tie-breaking/index-state sensitivity in keyword scoring. For reference, BM25 alone scores ≈0.665 nDCG@10 on SciFact (published BEIR baseline); this hybrid configuration sits in the range of strong modern dense retrievers. These numbers reflect the **generalizable retrieval core** (embedding model + Weaviate hybrid search), with the ISMA enrichment layers (HMM rerank / phi-tiling / query classifier) OFF — no corpus-specific tuning.

Reproduce (from the repo root, with the embedding server + Weaviate running):

```bash
PYTHONPATH=. BEIR_DATASET=scifact python3 benchmarks/beir_eval.py   # downloads the public BEIR set, writes benchmarks/results/scifact.json
```

*Note on prior numbers:* an internal evaluation harness (`isma/scripts/benchmark_retrieval.py`) also exists, but it runs against a private corpus with a small query set — those numbers are not reproducible or comparable across systems, so they are **not** reported here. The BEIR numbers above are the ones to trust.

---

## Quick start

```bash
# 1. Configure (localhost defaults work with the docker-compose stack below)
cp .env.example .env
pip install -r requirements.txt

# 2. Start Weaviate + Redis
docker compose up -d

# 3. Start the embedding server (Qwen3-Embedding-8B), or point EMBEDDING_URL
#    at any OpenAI-compatible /embed endpoint
./start.sh

# 4. Drop your own .md documents into demo/corpus/, then ingest
python3 demo/setup_demo.py

# 5. Query
python3 demo/setup_demo.py --query "your question here"
```

Minimum for the demo: 8 GB RAM + Docker. Configuration is via environment variables — see `.env.example`.

---

## Architecture

```
Clients → Query API (FastAPI, :8095)
              │
              ├── Weaviate          dense ANN (HNSW) + BM25 hybrid search
              │     └── Embedding endpoint   Qwen3-Embedding-8B (swappable)
              ├── Redis             filter-aware semantic cache
              └── Reranker (opt.)   Qwen3-Reranker-8B cross-encoder (off by default)

  Optional:   Neo4j               relational graph metadata
```

**Query pipeline:** `query → embed → hybrid (BM25 + vector) search → optional rerank → optional temporal weighting → top-k`.

---

## API

```bash
uvicorn isma.src.query_api:app --host 0.0.0.0 --port 8095
```

Read/search endpoints are open; write and operationally expensive endpoints require `ISMA_API_KEY` (sent as `X-API-Key`). `--host 0.0.0.0` is safe for writes because they are auth-gated.

This table is a partial list; see `isma/src/query_api.py` for the full surface.

Production runs the query API against the live Weaviate store on `http://localhost:8088`; the local `docker compose` demo still maps Weaviate on `8080`, so keep the two endpoints distinct when you reproduce or benchmark.

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/health` | Health check |
| GET  | `/stats` | Index stats |
| POST | `/search` | Hybrid (dense + BM25) search |
| POST | `/search/bm25` | Keyword-only search |
| POST | `/ingest/session` | Ingest a document/chunk (auth) |

```bash
curl -X POST http://localhost:8095/search \
  -H "Content-Type: application/json" \
  -d '{"query": "your query", "top_k": 10}'
```

---

## Key files

| File | Purpose |
|------|---------|
| `server.py` | FastAPI embedding inference server (Qwen3-Embedding-8B) |
| `isma/src/retrieval.py` | Core retrieval — dense + BM25, multi-scale |
| `isma/src/query_api.py` | HTTP query API |
| `isma/src/reranker.py` | Optional cross-encoder reranker client |
| `isma/src/query_classifier.py` | Query-type routing |
| `isma/src/semantic_cache.py` | Redis query cache |
| `benchmarks/beir_eval.py` | Public-dataset (BEIR) evaluation |
| `docker-compose.yml` | Weaviate + Redis stack |

---

## Install / hardware

Runs on any Linux machine with Docker and enough RAM for Weaviate + your embedding model. Developed on NVIDIA DGX Spark (GB10), but the demo runs on a laptop (8 GB RAM).

## License

MIT
