# ISMA authority v2 branch evaluation

Task: `isma-authority-v2::branch-eval`
Branch under eval: `agent/codex-correction-status-extension`
Code under eval: `a1723ef feat: log correction transition receipts`
Run timestamp: 2026-07-04T03:46:38Z
Weaviate endpoint: `http://127.0.0.1:8088`
Live class: `ISMA_Quantum`

## Scope

Bounded production-shape eval against the live `ISMA_Quantum` class. The run used a unique
throwaway `source_type=branch_eval`, `source_file=brancheval1e084ca15ffb_source`, lineage roots, and
exact UUID cleanup. It sampled the live vector shape (`4096` dimensions), wrote throwaway V1 tiles
to the real class, drove the branch/correction/contest transition methods, measured retrieval and
scoring behavior, then deleted every throwaway object.

Safety constraints:
- No live Neo4j mutation: `mark_revised(..., write_graph=False)` was used and `mark_contested`'s
  graph store was patched with a throwaway mock. This eval measured V1 tile state, retrieval, and
  eventlog receipts, not graph edge persistence.
- No live Redis mutation: `_embed_to_weaviate` used the real write path, but its embedding lookup was
  patched to return a vector sampled from the live class so the eval did not write embedding-cache
  keys/counters.
- Provenance scoring used `W_GRAPH=0` for the eval process so scoring did not query live Neo4j.

## Measures

| Measure | Result |
|---|---:|
| Live `ISMA_Quantum` count before eval | 1,563,977 |
| Live vector dimension sampled | 4096 |
| Throwaway objects created | 10 |
| Throwaway cleanup status | `204: 10` |
| Throwaway objects remaining after cleanup | 0 |
| False hard-supersede checks | 5 |
| False hard-supersede count | 0 |
| false_hard_supersede_rate | 0.0 |
| Legitimate prior deleted before cleanup | 0 |
| Correction prior retrievable with `include_superseded` | true |
| Eventlog receipts replayed | 4 |
| Eventlog receipt types | `BRANCH_CREATED`, `CORRECTION`, `BRANCH_CREATED`, `CONTRADICTION_DETECTED` |

False-hard-supersede checks:
- `mind_change_old`: `is_superseded=false`
- `recency_old`: `is_superseded=false`
- `contest_a`: `is_superseded=false`
- `contest_b`: `is_superseded=false`
- `correction_reject_before_refuter`: `is_superseded=false`

## Scenario results

### 1. Mind-change branches

Transition: `mark_revised([old], [new], evidence=..., write_graph=False)`.

Observed V1 state:
- Old tile: `correction_status=revised`, `authority=advisory`, `memory_zone=sandbox`,
  `is_superseded=false`, `superseded_by=""`, `invalidated_at=""`.
- New tile: `correction_status=current`, `authority=binding`, `memory_zone=canon`,
  `is_superseded=false`.
- Default retrieval returned both old and new tile IDs.
- Audit/include-superseded retrieval returned both old and new tile IDs.

Observed ranking:

| Mode | Rank 1 | Score | Rank 2 | Score |
|---|---|---:|---|---:|
| canon | new `current/binding/canon` | 0.820355 | old `revised/advisory/sandbox` | 0.763855 |
| audit | new `current/binding/canon` | 0.840035 | old `revised/advisory/sandbox` | 0.811735 |

Result: PASS. Both tiles remained retrievable; the current/canon successor ranked higher in canon
mode; the prior remained rankable in audit/history-style retrieval.

### 2. Correction hard-excludes only with refuter

Transition A: `_invalidate_superseded_tiles([old], new, timestamp, refuter=None)`.

Observed:
- Raised `ValueError` for missing refuter.
- Old tile remained `correction_status=current`, `is_superseded=false`, `superseded_by=""`.

Transition B: `_invalidate_superseded_tiles([old], new, timestamp, refuter={who, source, when})`.

Observed:
- Old tile became `correction_status=corrected`, `is_superseded=true`,
  `superseded_by=<new tile id>`, `invalidated_at=2026-07-04T03:46:38Z`.
- Old tile `provenance_hash` contained `event_type=CORRECTION`, `action=hard_supersede`,
  `old_tile_id`, `superseded_by`, timestamp, and the refuter fields.
- Default retrieval returned only the new tile.
- `include_superseded=true` retrieval returned both old and new tile IDs.

Result: PASS. Hard exclusion was rejected without a refuter and succeeded with a provenanced refuter;
the corrected prior was hidden only from default retrieval and preserved for audit/history retrieval.

### 3. Recency alone never hard-supersedes

Transition: `_embed_to_weaviate(event)` with the same `content_hash`/`lineage_root` as an existing
prior tile and no `refuter`.

Observed:
- `_embed_to_weaviate` returned `true`.
- Prior tile became `correction_status=revised`, `authority=advisory`, `memory_zone=sandbox`,
  `is_superseded=false`, `superseded_by=""`, `invalidated_at=""`.
- New shared-lineage tile was written as `correction_status=current`, `authority=binding`,
  `memory_zone=canon`, `is_superseded=false`.
- The prior was not hard-superseded.

Result: PASS. Shared-lineage recency produced a revision branch, not hard exclusion.

### 4. Contested surfaces both, down-weighted

Transition: `mark_contested(tile_a, tile_b, confidence=0.87, resolution=..., detected_by=...)`.

Observed:
- Both tiles became `correction_status=contested`, `is_superseded=false`, `superseded_by=""`,
  `invalidated_at=""`.
- Default retrieval returned both contested tile IDs.
- Provenance scoring reported `correction_obedience=0.6` for both contested tiles.
- Canon-mode scored results remained visible at `0.799355` each.

Result: PASS. Contested state did not hide either tile and applied the expected down-weight.

## Cleanup

Cleanup deleted every throwaway object created by the eval.

Observed cleanup summary:
- `created_count=10`
- `cleanup_status_counts={"204": 10}`
- `remaining_after_cleanup_count=0`
- `remaining_after_cleanup_ids=[]`

A follow-up query for `source_type=branch_eval` also returned zero objects after the failed-attempt
cleanup and after the passing run cleanup.

## Three-register

Observed:
- The live `ISMA_Quantum` class was reachable at `127.0.0.1:8088` and contained 1,563,977 objects
  before the eval.
- The eval wrote and deleted 10 throwaway V1 objects with exact cleanup.
- Mind-change, refuter correction, recency-only, and contested transitions produced the measured V1
  states and retrieval behavior above.
- `false_hard_supersede_rate=0.0`.
- No legitimate prior was deleted before cleanup.
- Eventlog replay reconstructed four transition receipts:
  `BRANCH_CREATED`, `CORRECTION`, `BRANCH_CREATED`, `CONTRADICTION_DETECTED`.

Inferred:
- On this bounded production-shape slice, the extension obeys the no-false-supersede invariant:
  mind-changes branch, recency alone branches, contested tiles remain visible, and hard exclusion
  happens only with a provenanced refuter.
- Because all prior IDs were still retrievable before cleanup, the extension preserved audit/history
  access for the evaluated prior tiles.

Unknown:
- This was not an exhaustive replay over all 1,563,977 existing live tiles.
- Live Neo4j edge persistence for `REVISES`/`CONTRADICTS` was not evaluated, by design, to avoid
  mutating live Neo4j during validation.
- The live Redis embedding cache path was not evaluated, by design, to avoid mutating live Redis
  during validation.
