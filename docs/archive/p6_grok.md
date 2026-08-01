# p6-grok-audit RE-AUDIT (branch e32bdf8)

**Date:** after weaver heads-up  
**Branch:** feature/memory-governance @ e32bdf8 (includes 3d14a37 fail-loud + e32bdf8 evidence/docs)  
**Re-audit scope:** re-pull + the fixes for the 3 prior BLOCKERs + minor #4 + evidence. Mandate unchanged.

## Commits since prior audit (the ones addressing the BLOCKERs)
- 3d14a37: fix: fail-loud supersede + doc drift (addresses BLOCKER 1 + 3)
- e32bdf8: docs+evidence: commit p4 production evidence + correct backfill claim (addresses BLOCKER 2)

(Plus prior 4de4815 boolean, 8c40c3a session stamping.)

## (1) SILENT FAIL-OPEN — FIXED (Observed)
Code now:
- _find_superseded_tile_ids: raises RuntimeError on RequestException, non-200, GraphQL errors. Query now selects `is_superseded` and filters result with `not obj.get("is_superseded")` (legacy string check removed).
- _invalidate_superseded_tiles: raises on RequestException or bad status.
These run before writing the new tile in _embed_to_weaviate.

Proven in p4_evidence RUN B (real dead Weaviate): raises "supersede lookup unreachable...". Write aborts cleanly.

**Inferred:** No more silent zombie on lookup/patch failure. Fail-loud / fail-closed as required.

## (2) NO COMMITTED P4 EVIDENCE — FIXED (Observed)
audit_logs/p4_production_evidence.md committed with 3 real-Weaviate runs (ephemeral container, branch code):

- RUN A: V1/V2 default excludes `is_superseded=true`; `include_superseded=true` includes it. ("current doc D v2" vs both).
- RUN B: fail-loud stress on dead Weaviate (connection refused) — raises as designed.
- RUN C: NotEqual true keeps `false` + `NOFLAG` (absent), excludes only `true`. Establishes BEIR no-regression by construction (filter only removes true; SciFact baseline has none).

**Inferred:** Evidence directly supports the exclusion, fail-loud, graceful, and "no regression by construction" claims. RUN C also proves the backfill correction.

## (3) DOC DRIFT — FIXED (Observed)
MEMORY_GOVERNANCE.md / AGENTS.md / KNOWN_FINDINGS.md now:
- Describe `is_superseded` (bool) as the eligibility flag the filter uses.
- State schema-presence is mandatory; values-backfill is optional (NotEqual true keeps un-flagged; verified in RUN C).
- Note both write paths stamp the fields.
- Supersede is fail-loud/fail-closed.

**Inferred:** Docs now match the boolean implementation and RUN C facts. Overstatement on backfill corrected.

## Minor #4 (legacy string in _find) — FIXED
_lookup now filters on `not obj.get("is_superseded")` (boolean). Good.

## Additional ruthless checks (on e32bdf8)
- Filter: only boolean `is_superseded NotEqual true` in _build_where/_build_filter. No remaining empty-string exclusion logic.
- Both write paths stamp: main `_embed_to_weaviate` + `/ingest/session` both set `is_superseded=false`, `valid_from`, `superseded_by=""`, `invalidated_at=""`, `provenance_hash`, `lineage_root` on new tiles. Supersede action (find+ invalidate) present in main path.
- /ingest/session: stamps for eligibility (so filter doesn't drop session tiles). Does not appear to call the supersede lookup/invalidate itself (consistent with it being a parallel "session summary" path rather than versioned document replace). If session writes can semantically supersede prior tiles, that action would need wiring — but per current design and policy it is not required for this use.
- History bypass: temporal-chain / reconstruct paths still use `follow_supersedes=True` where appropriate (unchanged, as required).
- Other excepts: parent linking and embed warning still swallow (non-critical to supersede correctness).
- Decay sweep: continues to use boolean predicate, fails loud.
- No over-engineering introduced in the fixing commits (narrow, targeted).
- Graceful degradation: intentional and documented + proven (RUN C).

## Three-register
**Observed:** The three BLOCKERs from the prior audit are addressed in code + committed evidence on e32bdf8. Boolean filter, fail-loud raises, consistent stamping in both paths, 3 real RUNs with pasted outputs, docs updated and backfill claim corrected. No remaining empty-string filter. p4 evidence present.

**Inferred:** The feature now demonstrably stops superseded tiles (default exclusion + fail-loud write-side) on the existing substrate. Production claims in the runbook/plan are supported by the committed evidence. "Without a new backbone" holds. Graceful degradation is correctly scoped.

**Unknown:** Whether every possible future /ingest/session use case will need explicit supersede action (current design treats it as a separate stamping path). Full queue end-to-end in one of the RUNs was not shown, but function-level + filter semantics + real Weaviate were exercised.

## Verdict
Original 3 BLOCKERs + minor #4 addressed. No new BLOCKERs identified in this re-audit.

The implementation + evidence now meet the mandate.

**No remaining BLOCKERs enumerated.**

(Full details in this file + the p4_evidence.md with the actual RUN outputs.)

