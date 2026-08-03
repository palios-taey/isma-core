# Taey system-prompt INDEX — weaver's section (ISMA: memory, retrieval, ingestion)

**Author:** weaver · 2026-07-28 · **Every pointer below was stat'd or run at authoring time** (per infra's constraint: a broken pointer is the disease). Verification receipts at the bottom.
**Canonical repo:** everything ISMA runs from the isma-core checkout (`$ISMA_HOME`) (public). Nothing runs from `embedding-server` — that consolidation is complete.

---

```
PROCESS:   ISMA prose retrieval — "what do we know / what did we say about X"
PLAN:      ISMA_PROSE_RETRIEVAL_SPEC.md   (VERIFIED 8174b)
           model-embedded surface: docs/taey/ISMA_MODEL_SURFACE_RETRIEVAL_SPEC_v1.md (VERIFIED 4527b)
           procedure (ALSO RETRIEVABLE, no filesystem needed — ask ISMA "how do I search ISMA for
           what we know about a topic"): ISMA_PROCEDURE_search_and_retrieval.md (VERIFIED 4425b)
LAUNCH:    already serving — no launch needed. Query it:
             CLI  : isma-query "<full-sentence question>"        (VERIFIED: isma-query on PATH)
             HTTP : POST http://localhost:8095/search {"query": "...", "top_k": 25}
             tool : search_isma (Taey's own tool, search_type=semantic)
           Seat: any. Service owned by weaver (isma-query-api.service, VERIFIED active+enabled).
EXPECT:    tiles returned with real scores (~0.4–0.7 typical on a good query) and the source_file
           of each. Success = the union across 2–4 phrasings visibly shapes what you write next.
           NOT-done = 0–2 thin snippets: that is a FAILED query, rephrase, do not proceed on it.
ON FAIL:   notify weaver. Review ISMA_PROSE_RETRIEVAL_SPEC.md FIRST — it decides the fork:
           wrong endpoint/route or dead service = BUG (weaver fixes); correct route but the query
           was a noun-bag / single phrasing / thin union = TRAINING gap (author a pair; the
           existing row is spec_knowledge/isma_query_form_v1.jsonl — extend, do not restate).
NEVER:     never use plain /v2/search, /search/hmm, /search/motif, or enriched_only=true for prose
           (they serve a partial shadow or hide the authored corpus).
           /v2/search/adaptive is supported; it is V1-based with a V2 overlay. Never hand-roll a
           raw Weaviate query that drops the default is_superseded filter. Never cite an ISMA
           number as a metric — ISMA is prose depth, cross-check numbers against
           treasurer/foundations/tech_baselines/INDEX.md.
```

```
PROCESS:   ISMA ingestion — putting an authored .md into the fleet's memory
PLAN:      CLAUDE.md (Ingest section, VERIFIED 5131b)
LAUNCH:    one file  : python3 isma/scripts/ingest_md_file.py <abs-path-to.md>   (VERIFIED present)
                       env: PYTHONPATH=$ISMA_HOME WEAVIATE_URL=http://localhost:8088
                            EMBEDDING_URL=http://localhost:8089/v1/embeddings NEO4J_URI=bolt://localhost:7689
           automatic : isma-md-corpus-watch.service (VERIFIED active+enabled) picks up watched roots
           nightly   : isma-nightly-ingest.timer    (VERIFIED live, next 03:00 daily)
           Seat: the author of the document, or weaver. cwd = $ISMA_HOME (the isma-core checkout).
EXPECT:    log line "inserted N/N" where the two numbers MATCH, AND the tiles are present in the
           store. A log line is NOT proof. VERIFY WITH FILTERS, NEVER WITH RANKING (measured
           2026-07-30): (a) persistence — query the store for source_file == <your path>, expect N
           tiles; (b) indexed — BM25 *restricted to that same source_file*, expect matches.
           Full procedure: ISMA_PROCEDURE_ingest_and_verify.md (also retrievable — ask ISMA
           "how do I ingest a document into ISMA and verify it worked").
           NEVER verify with an UNFILTERED ranked query — neither /search NOR /search/bm25. Rank
           depends on the other 1.6M tiles, not on whether your doc is there, so both will report a
           CORRECT ingest as failed. Measured: correct ingest at semantic rank 9-11 (0.35 vs 0.62-0.65);
           same doc's rare term at BM25 0.73 vs others 2.90-3.99 = absent from top-10 while fully
           indexed. "Rare in the world" is not "rare in this corpus" — ~70% of ISMA is transcripts
           that already discuss your distinctive term at length.
           NOT-done = "inserted N/M" with N<M, or 0 tiles present in the store.
ON FAIL:   notify weaver. Review isma-core/KNOWN_FINDINGS.md FIRST (VERIFIED 7591b) — it names the
           known traps: a full disk puts Weaviate READ-ONLY (writes fail while reads keep working),
           and the store returns HTTP 200 with per-object FAILED. Disk/read-only/route = BUG
           (weaver fixes); "I forgot to ingest it / ingested and never verified" = TRAINING gap.
NEVER:     never report an ingest as done from the log line alone. Never ingest anything containing
           a live credential or presigned URL (scrub first). Never ingest into any class other than
           ISMA_Quantum (V1).
```

```
PROCESS:   ISMA memory governance — corrections must beat stale drafts
PLAN:      MEMORY_GOVERNANCE.md   (VERIFIED present)
           procedure (ALSO RETRIEVABLE, no filesystem needed — ask ISMA "how do I correct a wrong
           document in ISMA"): ISMA_PROCEDURE_correcting_a_document.md (VERIFIED 4067b)
LAUNCH:    Correcting a document = edit the .md, re-ingest it — THEN hand-supersede the prior
           versions. ⚠ SUPERSEDE IS NOT YET AUTOMATIC (measured 2026-07-30): that fix is isma-core
           PR#11 and is NOT MERGED, so the running ingest script leaves the OLD version CO-CURRENT
           and fully retrievable. Observed live: one Taey-facing doc held 25 tiles across 2 versions
           (the old one carrying guidance already known WRONG); another held 40 stale tiles across
           MULTIPLE versions. FIXING THE FILE DOES NOT FIX THE MEMORY until PR#11 lands.
           By hand, until then: for every tile of that source_file whose doc_hash is NOT the new one,
           PATCH is_superseded=true + superseded_by=<new doc_hash> + correction_status=corrected.
EXPECT:    exactly ONE current (non-superseded) doc_hash remains for that source_file — that check is
           the proof, not the PATCH count. Compare FULL hashes: a 12-char prefix compared against a
           stored 16-char hash reports a false mismatch. NOT-done = 2+ current doc_hashes, i.e. the
           stale version is still competing with its own correction at equal weight.
           Note the default /search filter is `is_superseded NotEqual true`, which STILL RETURNS tiles
           whose flag is unset (verified) — so "unset" is NOT "hidden". Check the flag, not the ranking.
ON FAIL:   notify weaver. Review MEMORY_GOVERNANCE.md FIRST — supersede not firing = BUG (weaver;
           the supersede-on-reingest fix is isma-core PR#11); a human/model asserting a corrected
           fact that IS correctly marked superseded = TRAINING gap.
NEVER:     never set is_superseded=true by hand without a provenanced refuter (who declared it
           wrong, against what source, when). Never delete tiles to "fix" a wrong document —
           supersede it; the prior position stays retrievable for audit.
```

```
PROCESS:   Embedding server — the substrate retrieval AND ingestion both depend on
PLAN:      CLAUDE.md (Development section) + server.py at repo root
           procedure (ALSO RETRIEVABLE, no filesystem needed — ask ISMA "what do I do if the ISMA
           embedding server is down"): ISMA_PROCEDURE_embedding_server.md (VERIFIED 3245b)
LAUNCH:    systemctl --user start isma-embedding.service   (VERIFIED active; autostart ENABLED
           2026-07-28 by weaver — it was disabled, so it would NOT have come back after a reboot
           while query-api and the watcher would. Fixed this session.)
           Health: curl http://localhost:8089/health  → 200. Seat: weaver only.
EXPECT:    HTTP 200 on /health AND a real embed succeeding — /health returning 200 while embeds
           500 is the known silent-stale trap. embed-canary.timer (VERIFIED live, every 5 min)
           POSTs a real embed and restarts on non-200.
ON FAIL:   notify weaver (do not restart it yourself). Review isma-core/KNOWN_FINDINGS.md FIRST.
           Service down / canary firing = BUG (weaver). Everything is up but retrieval is thin =
           not this process; go to ISMA prose retrieval above.
NEVER:     never restart or reconfigure :8089 or :8088 without weaver — an ingestion run or a live
           search is likely mid-flight. Never set USE_COMPILE=true (torch.compile cudagraph crash
           caused a 2-day silent-stale outage). Never point ISMA at a different embedding model
           without a re-index plan — the stored vectors are Qwen3-Embedding-8B, 4096-dim.
```

---

## Verification receipts (all run 2026-07-28, this authoring session)
- Docs stat'd: ISMA_PROSE_RETRIEVAL_SPEC.md 8174b · ISMA_MODEL_SURFACE_RETRIEVAL_SPEC_v1.md 4527b · KNOWN_FINDINGS.md 7591b · isma-core/CLAUDE.md 5131b · MEMORY_GOVERNANCE.md present
- Scripts stat'd: ingest_md_file.py · nightly_ingest.py · watch_md_corpus.sh · backfill_md_corpus.py — all present
- CLI run: `isma-query` → returned 24 tiles at full_4096/top_k=25 (GO-DEEP defaults baked in)
- Endpoints run: `/search` 200 (2 tiles, top 0.45) · embedding :8089 200 · weaviate :8088 200
- Units checked: isma-query-api active+enabled · isma-md-corpus-watch active+enabled · isma-embedding active, **enabled by weaver this session** · embed-canary.timer live (5-min) · isma-nightly-ingest.timer live (03:00 daily)
- **Defect found + fixed while writing this index:** isma-embedding.service had no autostart link while its dependents did → on reboot, search and ingest would both fail with the query API up. `systemctl --user enable` applied, verified enabled, live service undisturbed. Exactly the class of dead-pointer rot this index exists to kill.
