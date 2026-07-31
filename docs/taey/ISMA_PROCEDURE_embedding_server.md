# The ISMA embedding server — what to do when ISMA search and ingest both fail

**Owner:** weaver (this service is weaver-only) · **Verified 2026-07-28 / 2026-07-30.**
The embedding server is the substrate that **both** ISMA retrieval and ISMA ingestion depend on. If search
and ingest fail *at the same time*, suspect this service first — a single failure here takes out both.

## What it is

Local embedding server on `http://localhost:8089`, serving **Qwen3-Embedding-8B at 4096 dimensions**, run as
the systemd user unit `isma-embedding.service` (entrypoint `server.py` at the `isma-core` repo root).

```bash
systemctl --user status isma-embedding.service
systemctl --user start  isma-embedding.service     # weaver only
curl -s http://localhost:8089/health                # expect 200
```

## How to check the ISMA embedding server is ACTUALLY working

**A 200 from `/health` does not prove the service works.** The known false-green: **`/health` returns 200
while real embed requests return 500.** That exact false-green caused a **two-day silent-stale ingestion
outage** — health looked fine the whole time while nothing was being embedded.

**Check it by performing a real embed, not by reading the health flag:**

```bash
curl -s -X POST http://localhost:8089/v1/embeddings \
  -H 'Content-Type: application/json' -d '{"input":"probe","model":"qwen3-embedding"}'
```

A real embedding vector back = working. This is automated: **`embed-canary.timer` fires every 5 minutes**,
POSTs a real embed, and restarts the service on a non-200. The canary exists *because* the health flag lied.

## Never do these to the ISMA embedding server

- **Never restart or reconfigure `:8089` or `:8088` without weaver.** An ingestion run or a live search is
  very likely mid-flight; a restart corrupts it silently.
- **Never set `USE_COMPILE=true`.** A `torch.compile` cudagraph crash caused the two-day silent-stale outage.
- **Never point ISMA at a different embedding model without a full re-index plan.** Every stored vector is
  Qwen3-Embedding-8B at 4096 dimensions; a different model silently produces garbage similarity against
  1.6M existing vectors — retrieval would degrade without any error surfacing.

## Known fragility, found and fixed 2026-07-28

`isma-embedding.service` was running but **`enabled=disabled`** — it had no autostart link while its
dependents (`isma-query-api`, `isma-md-corpus-watch`) did have one. On reboot the query API and the corpus
watcher would have come back and the embedding server would **not**, breaking all ISMA search and all ISMA
ingest while every dependent service reported healthy. Autostart is now enabled and verified. This is the
class of failure worth re-checking after any host change: **the dependency was less durable than its
dependents.**

## If the ISMA embedding server is down

**Notify weaver — do not restart it yourself.** Read `isma-core/KNOWN_FINDINGS.md` first. A service that is
down, or a firing canary, is a **BUG** that weaver fixes. If everything is up and healthy but ISMA retrieval
is merely *thin*, this is not the embedding server — that is a query-technique issue; see the ISMA search and
retrieval procedure (GO-DEEP: `top_k>=25`, 3–6 phrasings, union).
