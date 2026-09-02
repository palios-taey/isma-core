# ISMA ingest admission gate — design v2

**Status:** design, not implemented. Owner: weaver. Raised by treasurer-codex, relayed by tutor.
Custodian answers from tutor. **v2 rewrites the boundary in response to treasurer-codex's BLOCK
review of `61a45cf`** — v1 put the decision at the corpus write, which was the wrong boundary and is
the single largest change here.

**Read #59 first.** This document assumes its finding: the transcript ingest leg is inert because of
a broken shell-out, not because anything gates it.

---

## 0. What changed in v2, and why the boundary moved

v1 defined the decision *"at the point of corpus write."* treasurer-codex blocked it on two grounds
that share one root, and both are correct:

1. **It does not govern what #59 would restore.** `nightly_ingest.py:211-215` invokes the parser with
   `PARSED_DIR` as output. That repair writes **a new durable JSON copy and never calls Weaviate**. A
   Weaviate admission service cannot see it. v1's claim that this gate makes parser restoration safe
   was therefore **false as written**.
2. **Denied content crosses into the embedding service before a write-boundary gate could refuse it.**
   Measured: `ingest_md_file.py` reads at `:314`, tiles at `:351`, **embeds at `:364`**, writes at
   `:412`. `query_api.py` POSTs content to `EMBEDDING_URL` at `:973-979`, writes at `:1031`.
   `isma_core.py` tiles at `:1189`, embeds at `:1214`, writes at `:1253`.

> **So the decision moves to the SOURCE boundary: before any read, parse, copy, tile, embed, or
> write. A denied source must produce zero parser calls, zero embedding calls, and zero corpus
> writes.** Everything else in this document follows from that.

---

## 1. What this gate is, and what it is not

**It is:** a fail-closed admission decision taken at the point a source is first opened, keyed on
authenticated source identity and lineage — not on content.

**It is not** redaction or content screening. Nothing here inspects text for secrets or PII. Content
inspection cannot catch a compacted or paraphrased derivative of excluded material, and a gate that
*looks* like it screens content invites trust it cannot support.

**And an admit is narrow (§7).** It authorizes ingestion into the named ISMA sink for the named
purpose. It is not a privacy clearance, and it authorizes nothing else.

---

## 2. The write surface — corrected twice, by two reviewers

> **CORRECTION 1.** This section originally claimed ISMA had **one** corpus write choke point and
> labelled that "verified, not assumed." My enumeration regex required `.post(` and the URL on the
> *same line*; these calls are multi-line, so it returned a clean, plausible **false** negative that
> became v1's load-bearing feasibility argument. treasurer caught it.
>
> **CORRECTION 2, introduced by correction 1.** The rewrite dropped `disk_headroom_canary.sh` — it
> was listed in the original text and appeared **zero** times in the corrected text. treasurer-codex
> caught it. A correction is written with more confidence and read with less care than the claim it
> replaces, and that is where the next error lands.
>
> **Treat these counts as reviewed, not as verified by me.**

**Protected-class (`ISMA_Quantum`) writers, read directly:**

| site | endpoint | what it is |
|---|---|---|
| `ingest_md_file.py:172` | `POST /v1/batch/objects` | markdown corpus ingest |
| `query_api.py:1031` | `POST /v1/objects` | **network-exposed** `@app.post("/ingest/session")` |
| `isma_core.py:1195` | `POST /v1/objects` | multi-scale tiling write |
| `hmm_store_results.py:279` | `POST /v1/objects` | enrichment writeback |
| `validate_production.py:165` | `POST /v1/objects` | verification fixture |
| `verify_authority_filter.py:69` | `POST /v1/objects` | verification fixture |
| `disk_headroom_canary.sh:151` | `curl -X POST /v1/objects` | **shell**, live 15-min timer, probe object |

Excluded after checking the **verb** rather than the URL shape: `isma_core.py:798` is a PATCH
supersession loop; `/v1/objects/ISMA_Quantum/{id}` forms are GET/DELETE. Non-production:
`beir_eval.py`, `setup_demo.py`.

### Enforcement is a boundary, not a helper

A Python write helper cannot govern a `curl` in a shell script, and nothing stops the next writer, in
any language, from calling the raw endpoint. **A language-level helper is consolidation, not a
boundary.** Three parts, all required:

1. **Move non-corpus writers out of the protected class.** The canary probe and both verification
   fixtures belong in a dedicated probe/test class. That removes **three of the seven** and is the
   same logic as §7's fixture point: stop asking for the exemption rather than granting it.
2. **Enforce at a network/credential boundary**, so only the admission service holds credentials that
   can write the protected class — making the guarantee independent of language, future code, and
   external clients.
3. **A mechanical repository gate** banning direct protected-class writes, so a new raw POST fails CI
   rather than depending on a reviewer noticing.

**Deployment note:** the canary is one of the two *release-pinned* units, so a repo edit does not
change what runs until the pin moves (`deploy/pin_release.sh`, PR #61). Any plan that assumes editing
the script deploys it is wrong for this file specifically.

---

## 3. The AdmissionContext — decided at the source boundary

No current writer carries a trustworthy identity input, which is why the decision cannot be made from
existing payloads. Measured: `SessionTileRequest` makes `session_id` **optional** (`:938`), accepts a
**caller-supplied** `source_file` (`:935`), has **no** ancestry field, and stamps
`lineage_root=content_hash` (`:1015`). Markdown ingest derives its session from a **mutable
filename/path** (`ingest_md_file.py:47-88`). `ISMACore`'s event path uses content lineage, not
transitive session ancestry.

**These are caller-controlled tile properties. They cannot prove source identity.** The gate therefore
takes a separate, typed, provenance-attested input, constructed at the source boundary and never
populated from the payload being admitted:

| field | purpose |
|---|---|
| `source_kind` | session transcript / authored document / enrichment / probe — different identity rules |
| `source_id` | **immutable** source identity; for documents, trusted repo+commit+path provenance, not a mutable filename |
| `lineage_proof` | authenticated ancestor chain; fork / resume / compaction edges recorded **at creation** |
| `sink` + `purpose` | what this admit authorizes, and only that (§7) |
| `policy_revision` | which policy snapshot the decision was made against |

**Public document identity and session identity are distinguished deliberately** — a repo path with
commit provenance is a different kind of claim from a session ID, and collapsing them lets a mutable
path masquerade as an immutable identity.

**Missing, unverifiable, cyclic, or incomplete lineage refuses.**

---

## 4. Fail-closed contract, and conflict resolution

| condition | behaviour |
|---|---|
| policy source unset / unreadable / malformed | **refuse** — a policy you cannot read is not a policy |
| policy stale, unverifiable, rolled back, or tampered | **refuse** — same epistemic state as unreadable |
| source not covered by any rule | **refuse** — no implicit admit |
| lineage missing, forged, cyclic, or incomplete | **refuse** |
| required audit receipt cannot be written | **refuse** — an unrecordable decision is not a decision |
| positively admitted, no deny/hold anywhere in ancestry | admit, for the named sink and purpose only |

**Conflict resolution — deny dominates, transitively.** Simultaneous matches are *normal*, not an edge
case: admitting a corpus root while holding a descendant is the expected shape. So:

> **Any deny or hold on the subject, or on any transitive ancestor, dominates all admits. Only a
> positively admitted subject with no deny/hold ancestry may pass.**

**Consequence, flagged rather than buried:** wiring this in makes the currently-working markdown
prose ingest refuse everything until a policy explicitly admits the corpus roots. That is intended —
it converts #59's three accidental containments into declared decisions — but whoever lands it must
land an initial policy in the same change.

**And the stop must be distinguishable from the ingest simply being broken** (tutor). This fleet has
just spent a week on a unit exiting `0` over a leg that had ingested nothing for 52 days.
*"Refusing because no policy admits these roots"* and *"silently ingesting nothing"* must not look
alike to whoever glances at it next.

---

## 5. Decisions are recorded, and `HELD` alerts

Each decision records: policy snapshot identity and revision, the rule that matched, the
`AdmissionContext` evaluated, the lineage chain walked, timestamp, and outcome. **If the durable
receipt cannot be written, admission refuses** — see §4. Logged identifiers are minimised: record what
is needed to audit a decision, not the content or the private detail behind it.

**`HELD` means refuse-AND-ALERT** (tutor). The reasoning is the strongest argument in this document:

> A silent refusal is indistinguishable from a gate that never fired. If `HELD` refuses silently,
> then *"the gate is working and correctly refusing nothing"* and *"the gate is broken and refusing
> nothing"* produce identical observations, forever.

**Which forces a liveness requirement most gate designs omit: how would anyone notice the gate had
stopped firing at all?** Refusals are rare by design, so their absence is the expected steady state
and cannot be a liveness signal. This repo has solved this shape once — the disk canary writes a
heartbeat on *every completed observation*, including the healthy path, and a watchdog escalates on a
stale heartbeat. The admission service needs the same: a positive signal on every decision, and
something that notices when it stops.

---

## 6. Policy distribution — private canonical, published projection

A gate whose policy resolves only on one machine gives a downloaded Taey **no gate at all**, while
every code path still reports one is present. Per `CLAUDE.md` that is a disconnection violation, and
this is the worst place for one.

- **PRIVATE (canonical, operator-only):** full hold records — paths, receipts, reasons, provenance.
- **PUBLISHED (what ISMA consumes):** the minimum needed to refuse. **Never** paths, content, reasons,
  or filenames.

**The projection must not itself be a disclosure.** Publishing a list of held private session
identities leaks exactly what the hold protects. Prefer **opaque identifiers or admission
capabilities** over a public enumeration of private held identities.

**Integrity requirements:** signed/authenticated snapshots; monotonic version **plus a freshness bound**
(*"valid until T"*) so a consumer can refuse on its own clock without reaching the publisher; atomic
per-decision reads so a decision is never taken across a half-updated policy; refusal on rollback or
tamper. **Do not key freshness on content hash or size** of a living registry — it grows, and a
hash-keyed check goes stale silently, reproducing this gate's own failure one layer down.

**The projection must be derived from the canonical by a mechanical, reviewable transform**, never
hand-maintained (tutor). Two hand-maintained expressions of one rule is a future divergence with a
date on it — and this repo already has that prior art in the duplicated `_QUARANTINE_HASHES` pair.

### The existing read-side exclusion is the counterexample

```python
# isma/src/retrieval.py:77 and isma/src/retrieval_v2.py:48
# Must match the set in retrieval_v2.py — both V1 and V2 paths need filtering.
_QUARANTINE_HASHES = { "60c0df94b3a1271a", }
```

Content-hash keyed (will not survive paraphrase or re-tiling); hardcoded in source; duplicated across
two files with drift prevented by human diligence, which is not a mechanism; and **read-side only** —
the material is *in the store* and merely filtered from answers. It stays as defence-in-depth for what
is already there, but it is not the boundary.

---

## 7. Scope of an admit, and what is out of scope

**An admit authorizes ingestion into the named sink for the named purpose, and nothing else.** It does
**not** authorize training, publication, redistribution, or any other boundary change; each of those
requires its own content-minimisation / PII / secret control. This gate may exclude content scanning
from its implementation, but **it cannot be presented as the sole privacy authorization** — source
admission and content safety are different threats, and an explicitly-admitted source can still
contain third-party PII or secrets.

Also out of scope:

- **Retroactive reach.** The ~1,954 files checkpointed before 2026-07-09 are a realized historical
  exposure. No forward gate addresses them; that is a separate disposition.
- **Consumers outside isma-core.** Nothing in this repo reads `PARSED_DIR`. Whether anything outside
  does is **Unknown** and not closable from isma-core tooling.
- **The training-side registry itself.** tutor is custodian; ISMA is a consumer.

### Fixtures need a test class, not a carve-out

Three of the seven writers are non-corpus (canary + two fixtures). Under a fail-closed gate they are
refused unless policy admits them — and the answer to *"the gate broke the tests"* pressure is **not**
an exemption. A fixture that needs to write into the production class is asking for the wrong thing;
the fix is a test class (tutor). That removes the reason anyone would ask.

Measured while checking whether it has already cost anything, with a positive control because the
first attempt's control timed out and returned a clean, false empty — and with the store filter
treated as a **prefilter only**, because `source_file` is `tokenization=word` and a `Like` for
`*/__validate__/*` matched 436 ordinary corpus tiles for `.../vllm_engine/validate.py`:

| predicate | rows (exact match, in Python) |
|---|---|
| `__authority_filter_fixture__` | 0 |
| `/__fixture__/` | 0 |
| `/__validate__/` | 0 |
| positive control | 1 — instrument confirmed able to see |

Both fixture writers clean up by design, but cleanup lives in a `finally` that does not run on
`SIGKILL`. PR #64 adds a standing residue check; the permanent fix is the test class.

---

## 8. Blocked on — not open questions, blockers

1. **The canonical exclusion/hold registry DOES NOT EXIST.** tutor-owned, unstarted, and confirmed as
   such. No location, no schema, no published projection. This gate cannot be implemented against a
   registry that has not been authored, and neither of us will invent its contract in a thread.
2. **Lineage representation is undecided.** Requirements are settled (§3, recorded-at-creation,
   transitively closable, fail-closed on an incomplete walk). The edge-on-record vs separate-edge-set
   choice is named as a tradeoff and belongs to whoever reads the actual producers: an edge set that
   silently misses a write is indistinguishable from clean lineage, which fails toward admitting.
3. **Production authentication topology** after the §2 network boundary is Unknown.

---

## 9. How this gets verified

Not by tests I author — production is the oracle. The v1 list did not prove the load-bearing claims;
these, per treasurer-codex, do:

1. A held ancestor's **fork / resume / compaction descendant refuses**, under a fresh identity.
2. **Missing, forged, or incomplete lineage refuses.**
3. **Conflicting admit + deny refuses** (deny dominates transitively).
4. **Stale, replayed, or tampered policy refuses.**
5. **Required audit sink unavailable ⇒ refuses.**
6. A **denied source produces zero parse, zero embed, and zero store calls** — the §0 boundary claim.
7. A **raw direct `ISMA_Quantum` POST is rejected at the network boundary**, not merely absent from code.
8. The **liveness watchdog detects a stopped admission service** (§5).
9. Existing prose ingest continues to work under an explicit admit rule.

Each is a production observation with a receipt, not a green suite.
