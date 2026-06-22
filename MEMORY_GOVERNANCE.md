# Memory Governance

How this system decides **what** gets written to durable memory, **who** owns it, **when** it
decays, and **how** every write is provenanced. In a stateful memory system a bad *retrieval*
costs one turn, but a bad *write* can contaminate every future turn — so governance of the write
path and the validity model is the load-bearing discipline, not retrieval tuning.

This document is the policy. The mechanism that enforces it lives in the retrieval/ingest code
(see *Implementation mapping* below); the fields it relies on already exist on the tile record.

---

## The four governance questions

### 1. Write — what may enter durable memory
A durable write asserts something intended to be true later. It is reserved for:
- stated facts, preferences, and constraints;
- decisions and their rationale;
- corrections (which **supersede**, never silently overwrite — see §3).

It is **not** a dump of raw conversation, and it is **not** an automatic LLM extraction committed
without a check. Auto-extracting "facts" from a transcript and writing them unverified is the
single most dangerous pattern in a stateful memory system: an unverified claim, once written,
is retrieved as ground truth forever. **Writes pass a verify-before-write gate** — a human or an
explicit verification step stands behind a durable write. Curated-but-manual is a feature, not a
gap: it is the quality gate that keeps unverified content out of the valid set.

### 2. Ownership — scope classes
Every durable item has an ownership scope that determines its lifecycle:
- **session / working** — scoped to one working context; decays when that context closes.
- **operator / user** — personal context for one user; persists until the user revokes it.
- **canonical** — foundational facts that do not decay (constitutional/config-grade).

Scope is recorded on the item, not inferred at read time.

### 3. Decay — validity and supersession (the zombie-memory fix)
Every durable item carries **time-bounded validity**:
- `valid_from` — when the item became true.
- `superseded_by` — set when a newer version replaces it (empty = currently valid).
- `invalidated_at` — when it stopped being valid.

**Supersede-on-write:** writing a newer version of an item marks the prior version superseded
(it is *not* duplicated and *not* hard-deleted). **Decay classes:** session/working items decay on
context close; canonical items never decay; a superseded item is **excluded from retrieval**
immediately.

**Eligibility rule:** only currently-valid items are retrieved. A superseded or invalidated item is
**never delivered** into a working context — this is what prevents "zombie memory" (stale facts
re-surfacing and contaminating answers).

**History exception (must hold):** superseded items are *preserved*, not destroyed. The
temporal-chain / history-reconstruction path **deliberately traverses superseded items** to
rebuild the lineage of how a fact changed. The exclusion applies to *retrieval for answering*,
never to *history reconstruction* or *audit*.

### 4. Provenance — cannot-lie on every write
Every durable item carries a **`provenance_hash` = {source, content_hash, timestamp}**. This serves
two purposes:
1. **Audit / cannot-lie** — every stored claim traces to a source, or is labeled inference/unknown.
   A claim with no provenance is not durable-memory material.
2. **Change detection** — a delivery layer (below) can diff `provenance_hash` to detect when an
   item changed and needs re-delivering, without re-shipping unchanged content.

---

## Two layers: eligibility (what) vs delivery (when)

Memory governance and context delivery are separate concerns and must stay cleanly split:

- **Governance layer (this document):** owns the **valid set + its provenance** — which items are
  currently valid (§3) and their provenance (§4). It answers *what is eligible to be delivered*.
- **Delivery layer (the context assembler):** owns *when and how* the valid set is delivered into a
  working context — e.g. full at a context boundary, a lean pointer between boundaries — driven by
  the `provenance_hash` diff. It answers *delivery timing*, over the valid set this layer provides.

**Invariants both layers honor:**
- A superseded/invalidated item is never eligible, so it is never delivered.
- Provenance is present on every item.
- Delivery optimizations (pointers, lean modes) are *never* content loss — required context is
  delivered whole at a boundary and is one local read away otherwise. Required context is never
  silently truncated.

---

## Implementation mapping

The policy is enforced on the existing record schema and code paths — no new store or daemon:

| Concern | Where |
|---|---|
| Validity + provenance fields | `valid_from`, `superseded_by`, `invalidated_at`, `lineage_root`, `correction_status`, `provenance_hash` on the tile/memory record |
| Supersede-on-write | the single ingest entrypoint marks prior matching versions superseded + stamps `provenance_hash` before committing the new item |
| Eligibility filter (read) | the query filter excludes `superseded_by != ""` by default, so superseded items are never retrieved |
| History / reconstruction | the temporal-chain and session-reconstruction paths **bypass** the eligibility filter to traverse superseded items |
| Provenance scoring | retrieval scoring already reads `superseded_by` / `correction_status`; governance promotes this from a down-weight to an eligibility gate |

---

## Three-register discipline

Every durable claim is labeled, in the spirit of cannot-lie provenance:
- **Observed** — verified against a source/measurement.
- **Inferred** — pattern-supported but not proven.
- **Unknown** — genuinely undetermined; held open, not resolved.

A claim that cannot be traced to a source is labeled inference or unknown — it does not enter
durable memory as fact.
