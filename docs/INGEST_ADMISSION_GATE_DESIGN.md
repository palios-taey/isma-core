# ISMA ingest admission gate — design v3

**Status:** design, not implemented. Owner: weaver. Raised by treasurer-codex, relayed by tutor.
Custodian answers from tutor. Reviewed to BLOCK twice by treasurer-codex; **v3 answers blockers A–D
on `1011983`.**

**Read #59 first.**

---

## 0. Revision history — what each round got wrong

| version | the error, and who caught it |
|---|---|
| v1 | Claimed **one** corpus write choke point, labelled "verified, not assumed." An enumeration regex required `.post(` and the URL on the same line; the calls are multi-line. **treasurer** |
| v1→v2 | The correction **dropped** `disk_headroom_canary.sh`, which the original text had listed. Seven writers, not six. **treasurer-codex** |
| v2 | Put the decision at the **corpus write**, which does not govern the `PARSED_DIR` copy #59 restores, and arrives after content reaches the embedding service. **treasurer-codex** |
| v3 | *(this revision)* Applied "boundary, not helper" to Weaviate **only**, leaving every other side effect a convention; no binding between admitted metadata and the bytes later processed; lineage modelled as a chain when it is a DAG; liveness that cannot distinguish idle from stopped or from bypassed. **treasurer-codex** |

**Four rounds, four load-bearing errors, none of them found by me.** That is the honest status of this
document: treat its claims as reviewed, never as verified by its author.

---

## 1. What this gate is

A **fail-closed admission decision**, taken before a source produces *any* side effect, keyed on
authenticated source identity and lineage — never on content.

It is **not** redaction or content screening: content inspection cannot catch a compacted or
paraphrased derivative of excluded material. And an admit is narrow (§7) — it authorizes one sink for
one purpose and is not a privacy clearance.

---

## 2. The enforcement model — every effect boundary, not just Weaviate

v2 correctly argued that a language-level helper cannot be a boundary, then **applied that only to
`ISMA_Quantum`**. Every other side effect was left as a convention — which is the same failure one
layer out. The covered effects, all verified in source:

| effect | where | today |
|---|---|---|
| remote transcript transfer | `nightly_ingest.py:238` (Mac), `:264` (Jetson) | `rsync` lands content in staging **before** anything can evaluate it |
| parse → new durable copy | `nightly_ingest.py:211-215` → `PARSED_DIR` | no boundary at all; this is the copy #59 restores |
| incoming move/copy | `:321`, `:335`, `:342` | moves originals and copies corpus regardless of parse outcome |
| embedding | `EMBEDDING_URL`, called from **12 files** | a raw network endpoint reachable by anything on the host |
| protected-class write | seven sites (§3) | raw `POST` reachable by anything with the endpoint |

**Requirement.** Either **(a)** the admission service owns the complete source adapter and is the only
OS identity permitted to write staging / `PARSED_DIR` / corpus, and the only network principal
permitted to call the embedding and store services; or **(b)** every parse, copy, embed and write
requires a signed capability (§4) with raw access denied at the filesystem and network boundary.

Mechanical repository gates must cover **direct parser invocation, protected copies, and embedding
calls** — not only raw `ISMA_Quantum` POSTs.

**Remote sources cannot be admitted after transfer.** `rsync` *is* the effect. Admission must happen
producer-side, or against a **signed remote manifest**, before bytes move.

---

## 3. The protected-class writers — corrected twice, receipts re-verified

| site | endpoint | note |
|---|---|---|
| `ingest_md_file.py:172` | `POST /v1/batch/objects` | markdown corpus ingest |
| `query_api.py:1031` | `POST /v1/objects` | **network-exposed** `@app.post("/ingest/session")` |
| `isma_core.py:1253` | `requests.post(url, …)` | the POST; `:1195` is only where the URL string is built |
| `hmm_store_results.py:279` | `POST /v1/objects` | enrichment writeback |
| `validate_production.py:165` | `POST /v1/objects` | verification fixture |
| `verify_authority_filter.py:69` | `POST /v1/objects` | verification fixture |
| `disk_headroom_canary.sh:166` | `curl -X POST` | **shell**, live 15-min timer |

> **Both corrected citations were mine and had different causes.** `isma_core.py:1195` named URL
> *construction* rather than the call — and v2's own §0 cited `:1253` correctly, so the document
> disagreed with itself. `disk_headroom_canary.sh:151` was read from the **pinned release**, not repo
> HEAD: #58 added a comment block that shifted the `curl` to `:166`. I had two versions of one file in
> play and cited the deployed one while documenting the repo — in a document whose own subject is the
> gap between what is merged and what runs.

**Three of the seven are non-corpus** (canary + two fixtures) and belong in a probe/test class (§7).

---

## 4. AdmissionContext, and the AdmissionDecision that binds it to bytes

**Context is the question; the capability is the answer.** v2 defined only the former, which leaves a
TOCTOU hole: a caller admitted for `source_id` A can swap the file, symlink, request body, or session
snapshot before parse, embed, or write.

**`AdmissionContext`** — constructed at the source boundary, never populated from the payload:

| field | note |
|---|---|
| `source_kind` | session transcript / authored document / enrichment / probe |
| `source_id` | **immutable**; for documents the attested `repo+commit+path`, never a mutable worktree path |
| `source_version` | authenticated snapshot / version / byte-range for a living session |
| `lineage_proof` | §5 |
| `sink` + `purpose` | what is being requested, and only that |

**`policy_revision` is decision OUTPUT, not context input.** The evaluator selects the current fresh
snapshot and stamps it on the receipt. Letting the caller name a revision invites **stale-policy
pinning** — asking to be judged by an old rulebook. *(This was wrong in v2's §3 table.)*

**`AdmissionDecision` / capability** — signed or service-internal, **single-use, non-replayable,
short-lived**, bound to: `source_kind` + immutable source version handle + complete lineage proof +
`sink`/`purpose` + the policy revision it was decided under + expiry and nonce.

Every downstream stage — parse, copy, embed, write — **refuses a missing, mismatched, replayed, or
expired capability.**

- **Repo documents:** open the attested **git blob** (`repo+commit+path`). A worktree path is mutable
  and cannot be the thing opened.
- **Living sessions:** bind to an authenticated snapshot / version / byte range. **A `session_id`
  alone does not name an immutable byte version.**
- **The query API is the sharp case:** FastAPI reads and parses the content-bearing request body
  before handler code runs, so admission metadata must be authenticated **out-of-band** — a check
  inside the handler is already too late.
- **A content digest may bind bytes against TOCTOU without becoming the policy identity.** These are
  different jobs: identity decides *whether*, the digest proves *the same bytes*. Conflating them
  reintroduces content-keyed policy, which §7 rejects.

---

## 5. Lineage is a DAG, and completeness must be provable

v2 said "ancestor chain." **Compaction and handoff can combine multiple contexts, so the structure is
a DAG with possibly many parents.** More importantly: *"incomplete refuses"* is unimplementable unless
the verifier can tell a **true root** from a **missing parent** — and a missed edge is
indistinguishable from clean lineage, failing toward admit.

**Requirement:** authenticated **complete-parent-set** records, plus an explicit **signed root
certificate**, both written **atomically when each identity is created** — never inferred afterwards,
because nothing in a descendant's own bytes reveals its ancestry. The gate walks the full ancestor
DAG. **Absent root/parent completeness proof, unresolved parents, or cycles ⇒ refuse.**

**Capability lease vs new holds:** a hold created after a capability was issued must invalidate it, or
the capability must expire within a **stated maximum lease**. An unbounded capability outliving the
hold that should have stopped it is a silent admit.

---

## 6. Fail-closed contract and conflict resolution

| condition | behaviour |
|---|---|
| policy unset / unreadable / malformed / stale / rolled back / tampered | **refuse** |
| source matched by no rule | **refuse** — no implicit admit |
| lineage missing, forged, cyclic, or incomplete | **refuse** |
| capability missing, mismatched, replayed, or expired | **refuse** |
| required audit receipt cannot be written | **refuse** — an unrecordable decision is not a decision |
| positively admitted, no deny/hold anywhere in the ancestor DAG | admit, for that sink and purpose only |

> **Deny or hold on the subject, or on ANY transitive ancestor, dominates all admits.** Overlap is
> normal, not exceptional: admitting a corpus root while holding a descendant is the expected shape.

**Consequence, flagged not buried:** this makes the currently-working prose ingest refuse everything
until a policy admits the corpus roots. Intended — it converts #59's accidental containments into
declared decisions — but the initial policy must land in the same change, and **the stop must be
distinguishable from the ingest simply being broken** (tutor). This fleet just spent a week on a unit
exiting `0` over a leg that had ingested nothing for 52 days.

**Policy distribution.** Private canonical (full records: paths, receipts, reasons) vs published
projection (the minimum needed to refuse — never paths, content, reasons, or filenames). **The
projection must not itself be a disclosure:** publishing held private identities leaks what the hold
protects, so prefer **opaque identifiers or admission capabilities** over any public enumeration.
Signed snapshots; monotonic version **plus a freshness bound** so a consumer refuses on its own clock;
atomic per-decision reads; refusal on rollback or tamper. **Never key freshness on content hash or
size** of a growing registry. The projection is derived from canonical by a **mechanical, reviewable
transform**, never hand-maintained (tutor) — this repo's duplicated `_QUARANTINE_HASHES` pair, kept in
sync by a "must match" comment, is the prior art for why.

---

## 7. Scope of an admit

**An admit authorizes ingestion into the named sink for the named purpose and nothing else** — not
training, not publication, not redistribution, not any other boundary change, each of which needs its
own content-minimisation / PII / secret control. This gate may exclude content scanning from its
implementation, but **it cannot be presented as the sole privacy authorization**: an explicitly
admitted source can still contain third-party PII or secrets.

Out of scope: retroactive reach (the ~1,954 files checkpointed before 2026-07-09 are a realized
historical exposure needing separate disposition); consumers outside isma-core (**Unknown**); the
training-side registry itself (tutor is custodian).

**Fixtures need a test class, not a carve-out.** Under a fail-closed gate the canary and both fixtures
are refused unless policy admits them, and the answer to *"the gate broke the tests"* is not an
exemption — a fixture that needs to write the production class is asking for the wrong thing (tutor).
Measured residue today, with the store filter used only as a prefilter because `source_file` is
`tokenization=word` and a `Like` for `*/__validate__/*` matched 436 ordinary corpus tiles for
`.../vllm_engine/validate.py`: **0 / 0 / 0**, positive control 1. PR #64 makes that a standing check.

---

## 8. Blocked on

1. **The canonical exclusion/hold registry DOES NOT EXIST** — tutor-owned, unstarted, confirmed. No
   location, no schema, no projection. Neither of us will invent its contract in a thread.
2. **Lineage representation undecided** — requirements settled (§5); the record/edge-set choice
   belongs to whoever reads the actual producers.
3. **Production authentication topology** after §2's boundary — **Unknown**.

---

## 9. Verification — production observations, not a suite

1. Held ancestor ⇒ **fork / resume / compaction descendant refuses** under a fresh identity.
2. **Multi-parent** held ancestry refuses (DAG, not chain).
3. Missing, forged, or incomplete lineage refuses; **a true root is distinguishable from a missing parent.**
4. Conflicting admit + deny refuses (deny dominates transitively).
5. Stale, replayed, or tampered policy refuses.
6. Capability **replay** and **payload-swap** refuse.
7. Required audit sink unavailable ⇒ refuses.
8. A denied source produces **zero parse, zero copy, zero embed, zero store** — *observed at every
   adapter and derivative path*, not only the Weaviate one.
9. Raw direct **`ISMA_Quantum` POST**, raw **`PARSED_DIR` write**, and raw **embedding call** are each
   rejected at the filesystem/network boundary — not merely absent from code.
10. **Liveness:** fixed-cadence service heartbeat **plus** scheduled end-to-end admission canaries
    through each adapter. A heartbeat alone cannot distinguish idle-healthy from stopped, and a
    service can heartbeat happily while an adapter bypasses it entirely.
11. Existing prose ingest still works under an explicit admit rule.
