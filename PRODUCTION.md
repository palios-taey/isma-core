# PRODUCTION — what ISMA actually is

**The code is the truth.** Every line below was derived by observing the running system on
2026-07-31: reading the systemd units to find the entry points, importing those entry points and
inspecting `sys.modules`, and probing live requests. Nothing here is claimed from the repo layout,
a filename, or a doc.

**There is one version of each thing.** If you find two, the one listed here is production and the
other is a defect — say so rather than choosing.

Regenerate the classification: `python3 isma/scripts/validate_production.py` proves the capabilities;
`python3 isma/scripts/generate_schema_docs.py` regenerates the schema reference.

---

## 1. The seven entry points

Everything that runs, taken from `systemctl --user show -p ExecStart`:

| Unit | Runs | State |
|---|---|---|
| `isma-query-api` | `isma.src.query_api:app` via uvicorn on **:8095** | service, always on |
| `isma-embedding` | `server.py` on **:8089** | service, always on |
| `isma-md-corpus-watch` | `isma/scripts/watch_md_corpus.sh` | service, always on |
| `isma-nightly-ingest` | `isma/scripts/nightly_ingest.py --skip-rsync` | timer, 03:00 |
| `embed-canary` | `isma/scripts/embed_canary_healthcheck.sh` | timer, 5 min |
| `isma-disk-canary` | `isma/scripts/disk_headroom_canary.sh` | timer, 15 min |
| `isma-backup` | `isma/scripts/backup_isma_store.sh` | timer, 04:30 |

Plus one entry point invoked by clients rather than systemd:

| Surface | File | Invoked by |
|---|---|---|
| MCP stdio server | `isma/src/mcp_server.py` | MCP clients — **this is Taey's tool path** |

**An entry point is never imported.** Do not use "imported by 0" to judge whether one is alive.

## 2. The serving code

**Loaded at import time by the two live services (21 modules)** — verified by importing
`isma.src.query_api` and `server` and reading `sys.modules`:

```
isma/__init__.py            isma/src/hmm/__init__.py      isma/src/isma_core.py
isma/config.py              isma/src/hmm/eventlog.py      isma/src/phi_tiling.py
isma/src/__init__.py        isma/src/hmm/gate_b.py        isma/src/query_api.py
isma/src/breathing_cycle.py isma/src/hmm/ids.py           isma/src/relational_lens.py
isma/src/functional_lens.py isma/src/hmm/motifs.py        isma/src/retrieval.py
isma/src/temporal_lens.py   isma/src/hmm/neo4j_store.py   isma/src/semantic_cache.py
server.py                   isma/src/hmm/query.py         isma/src/hmm/redis_store.py
```

**Loaded lazily, on request, inside route handlers (8 modules)** — equally production; they are
absent from an import-time snapshot only because they load when a request needs them:

```
isma/src/agentic_retry.py           isma/src/relational_retrieval.py
isma/src/contradiction_detector.py  isma/src/reranker.py
isma/src/provenance_scorer.py       isma/src/retrieval_v2.py
isma/src/query_classifier.py        isma/src/temporal_query.py
```

## 3. The data

| Store | Where | Contents |
|---|---|---|
| Weaviate | `:8088`, data at `/var/spark/weaviate-isma` | class **`ISMA_Quantum`**, ~1.6M tiles, **73 properties** |
| Neo4j | `bolt://localhost:7689`, data at `/home/mira/neo4j-isma-data` | graph enrichment; core search runs without it |
| Redis | cache + HMM inverted index | optional |

The full field list with types and semantics is `docs/taey/ISMA_SCHEMA_REFERENCE.md`, **generated
from the live class** — never hand-edited. Regenerate it after any schema change.

## 4. The query surface

| Route | Status |
|---|---|
| `POST :8095/search` | **CANONICAL** for prose |
| `POST :8095/search/bm25` | **CANONICAL** for exact terms |
| `GET :8095/document/{content_hash}/text` | **CANONICAL** for expansion |
| `POST :8095/v2/search/adaptive` | **SUPPORTED** — V1-based with a V2 overlay |
| `POST :8095/v2/search` | **DEPRECATED → HTTP 410**, returns a pointer to `/search` |
| `POST :8095/search/hmm`, `/search/motif` | **NOT for prose** — HMM-gated; authored prose is `hmm_enriched=false` |

Full routing rule with measurements: `ISMA_PROSE_RETRIEVAL_SPEC.md`.

## 5. Operator tools — real, but NOT the serving path

Run on demand; nothing serves from them. They are not dead code and not production runtime:

`isma/scripts/`: `backfill_md_corpus.py` · `benchmark_retrieval.py` · `colbert_retrieval.py` ·
`decay_sweep.py` · `generate_schema_docs.py` · `hmm_package_builder.py` · `hmm_store_results.py` ·
`ingest_md_file.py` (also invoked by the watcher) · `restore_verify_isma.py` ·
`validate_production.py` · `verify_authority_filter.py`
· plus `benchmarks/beir_eval.py` and `demo/setup_demo.py`.

## 6. How this was determined, so it can be re-derived rather than trusted

1. `systemctl --user show -p ExecStart` for every ISMA unit → the entry points.
2. Import each entry point, diff `sys.modules` → the import-time closure.
3. Grep for `from isma.src.X import` inside handler bodies → the lazy-loaded set.
4. Anything reachable by neither, and invoked by nothing, is not production.

**Three traps this process had to survive**, each of which produced a wrong answer first:
- *An entry point is never imported.* A pure "imported by 0" test marks `mcp_server.py`,
  `nightly_ingest.py` and every CLI as dead.
- *A static AST closure misses lazy imports.* It marked all 8 request-time modules as unreachable;
  they are production and load on the first request that needs them.
- *A match count is not content.* Counting occurrences of a name says a string exists, never what
  the line means.
