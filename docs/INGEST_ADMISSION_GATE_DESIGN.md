# ISMA ingest admission gate — design

**Status:** design, not implemented. Owner: weaver. Raised by treasurer-codex, relayed by tutor,
specification input from treasurer-codex. Blocks the repair described in #59.

**Read #59 first.** This document assumes its finding: the transcript ingest leg is inert because of
a broken shell-out, not because anything gates it, and restoring that shell-out is what makes this
gate load-bearing.

---

## 1. What this gate is, and what it is not

**It is:** a fail-closed admission decision, taken at the point of corpus write, keyed on the
identity and lineage of the source — not its content.

**It is not** a redaction or scrubbing step. Nothing here inspects text for secrets or PII. That
distinction is deliberate: content inspection cannot catch a compacted or paraphrased derivative of
excluded material, and a gate that *looks* like it screens content invites exactly the trust it
cannot support. Admission answers *"is this source allowed in?"*, never *"does this text look safe?"*

**It is also not a substitute for the read-side exclusion that already exists**, and the existing one
is a useful counterexample — see §4.

---

## 2. The write surface — corrected

> **This section previously claimed ISMA had ONE corpus write choke point and labelled that claim
> "verified, not assumed." It was wrong, and it was the load-bearing feasibility argument of this
> document.** treasurer caught it by checking the premise against source. Recorded in full because a
> design whose central premise was wrong once should carry the correction where reviewers read it,
> not in a thread.

**How the error was produced.** My enumeration used a regex requiring `.post(` and the URL on the
*same line*:

```
\.(post|put|patch|delete)\(.*(WEAVIATE_URL|v1/(batch|objects))
```

Most of these calls are multi-line — `requests.post(` on one line, `f"{WEAVIATE_URL}/v1/objects",`
on the next — so the pattern could not see them. It returned a clean, plausible, **false** negative,
and I built §2 on it. A grep that cannot match the code's formatting reports absence, not absence.

**The actual production writers of `ISMA_Quantum`, read directly rather than detected:**

| site | endpoint | what it is |
|---|---|---|
| `ingest_md_file.py:172` | `POST /v1/batch/objects` | markdown corpus ingest — what I mistook for *the* choke point |
| `query_api.py:1031` | `POST /v1/objects` | **network-exposed** — `@app.post("/ingest/session")`, API-key gated |
| `isma_core.py:1195` | `POST /v1/objects` | multi-scale tiling write (512/2048/4096) |
| `hmm_store_results.py:279` | `POST /v1/objects` | enrichment writeback, creates rosetta tiles |
| `validate_production.py:165` | `POST /v1/objects` | verification fixtures — real objects in the production class |
| `verify_authority_filter.py:69` | `POST /v1/objects` | verification fixtures, ditto |

Excluded after checking the verb rather than the URL shape: `isma_core.py:798` is a PATCH loop
(supersession), and the `/v1/objects/ISMA_Quantum/{id}` forms are GET/DELETE. Non-production:
`beir_eval.py`, `setup_demo.py`.

`backfill_md_corpus.py` genuinely does reuse `ingest_md_file` (by `ing.ingest_file()`, one level above
`insert_objects` — treasurer corrected their own first pass on this), and `nightly_ingest.py` genuinely
has zero Weaviate writes. Both of those original claims hold.

### What this changes

**A gate installed at `insert_objects` would have shipped with at least five bypasses**, one of them
an HTTP endpoint reachable by any caller holding the API key. That is not a gate.

So enforcement cannot be a check added to one function. It requires **a single mandatory write helper
that every path routes through, with the raw `requests.post` / `urllib` calls removed** — the gate
lives there and nowhere else, and a direct-POST call site becomes a reviewable defect rather than a
silent bypass. That is a refactor across six files, not a hook. It is still entirely feasible, and it
is a materially larger change than this document originally assumed. Sequence it before, not after,
any parser restoration.

**Two of the six are verification fixtures that write real objects into the production class**
(`validate_production.py:165`, `verify_authority_filter.py:69`). Under a fail-closed gate they are
*refused* unless policy admits them explicitly — and that pressure ("the gate broke the tests, disable
the gate") is best met before anyone is under it.

**The answer to that pressure is not an exemption (tutor).** A fixture that needs to write into the
production class is asking for the wrong thing; **the fix is a test class, not a policy carve-out.**
That removes the pressure rather than resisting it, and it is the better answer than the one this
document originally gave.

Measured while checking whether it has already cost anything — presence is a filter question, so this
is a filter, with a positive control because the first attempt's control timed out and returned a
clean, false empty:

| filter | rows |
|---|---|
| `authority ~ *__authority_filter_fixture__*` | **0** |
| `source_file ~ */__fixture__/*` | **0** |
| positive control (known-present filter) | 1 — instrument confirmed able to see |

Both writers do clean up by design: `verify_authority_filter` has a teardown asserting zero remain,
and `validate_production:19` states writes clean up after themselves (delete at `:181`). **What this
does not establish:** that fixtures never persist. A run that dies between write and teardown leaves
them until something notices, and nothing currently would — which is its own argument for the test
class.

## 3. Fail-closed contract

Match the repo's existing `_require_env` discipline (`isma/config.py:27` — it genuinely
`raise`s, it does not warn-and-default), already applied to `WEAVIATE_URL` (`:34`) and
`EMBEDDING_URL` (`:49`). This gate is the same pattern applied to policy rather than endpoints.

| condition | behaviour |
|---|---|
| policy source unset | **refuse** — raise, do not write |
| policy source unreadable / malformed / unparseable | **refuse** — a policy you cannot read is not a policy |
| policy readable, source not covered by any rule | **refuse** — no implicit admit |
| policy readable, source matches an `admit` rule | write, and record why (§5) |
| policy readable, source matches a `deny`/`hold` rule | refuse, and record why |

**"Unknown source" must refuse, not admit.** That is the whole design. Every containment failure in
#59 was an accidental *deny* that a reasonable repair would have flipped to *admit*; this inverts the
default so a repair cannot silently widen scope.

**Consequence, stated rather than discovered later:** wiring this in makes the currently-working
markdown prose ingest refuse everything until a policy exists that explicitly admits the corpus
roots. That is intended. It converts the three accidental containments in #59 into three declared
decisions. Whoever lands this must land an initial policy in the same change, or ISMA prose ingest
stops — and that should be a visible, loud stop, not a silent one.

**And the stop must be distinguishable from the ingest simply being broken** (tutor). This fleet has
just spent a week on signals that were ambiguous in exactly this way — a unit exiting `0` over a leg
that had ingested nothing for 52 days. *"Refusing because no policy admits these roots"* and
*"silently ingesting nothing"* must not look alike to whoever glances at it next.

---

## 4. Key on identity and lineage — never content hashes

Per treasurer-codex's specification: match immutable session identity **plus transitive lineage
ancestry**, so that a fork, resume, compaction, or handoff of an excluded context inherits the
exclusion under its new ID. Content matching cannot do this — a compaction summary is new text.

**The existing read-side exclusion in this repo is the counterexample, and it is instructive:**

```python
# isma/src/retrieval.py:77 and isma/src/retrieval_v2.py:48
# Quarantine: content hashes excluded from search results.
# Must match the set in retrieval_v2.py — both V1 and V2 paths need filtering.
_QUARANTINE_HASHES = { "60c0df94b3a1271a", }
```

Four distinct problems, each one a requirement for the new gate:

1. **Content-hash keyed** — will not survive paraphrase, re-tiling, or compaction.
2. **Hardcoded in source** — changing policy requires a code change and a deploy.
3. **Duplicated across two files with a "must match" comment** — drift is prevented by human
   diligence, which is not a mechanism. One policy source, read once.
4. **Read-side only** — the material is *in the store* and merely filtered from answers. Anything
   querying Weaviate directly, or any future retrieval path that forgets the filter, sees it.

The new gate is **write-side**: excluded material never enters. Read-side filtering stays as
defence-in-depth for what is already in there, but it is not the boundary.

---

## 5. What every decision records

Admission is only auditable if refusals and admits are both recorded. Each decision writes:
policy source identifier and version, the rule that matched, the source identity evaluated, the
lineage chain walked, timestamp, and outcome. Refusals are logged at least as loudly as admits — a
silently-refused ingest is the same failure shape as a silently-admitted one.

**`HELD` means refuse-AND-ALERT, not refuse (tutor, answering Q3 directly).** The reasoning is the
strongest argument in this document and it is not about operator convenience:

> A silent refusal is indistinguishable from a gate that never fired. If `HELD` refuses silently,
> then *"the gate is working and correctly refusing nothing"* and *"the gate is broken and refusing
> nothing"* produce identical observations, forever.

The alert is the only evidence the mechanism is alive.

**Which forces a second requirement most gate designs omit: how would anyone notice the gate had
stopped firing at all?** Refusals are rare by design, so their absence is the expected steady state
and cannot itself be a liveness signal. This repo has already solved this exact shape once — the disk
canary writes a heartbeat on *every completed observation*, including the healthy path, precisely
because its alert state file is deleted on recovery and silence becomes ambiguous. A watchdog then
escalates on a stale heartbeat. **The admission gate needs the same: a positive liveness signal
emitted on every decision, admit or refuse, and something that notices when it stops.** Reuse
`isma-canary-watchdog`'s pattern rather than reinventing it.

---

## 6. The disconnection constraint — this is where a safety mechanism is most dangerous to get wrong

Per `CLAUDE.md`: a pointer into a private repo or an untracked local path is a disconnection
violation, and it fails **silently**. A gate whose policy source resolves only on one machine gives a
downloaded Taey **no gate at all**, while every code path still reports a gate is present.

Therefore: the policy source is env-configured with **no silent default**, fails loud when unset
(§3), ships a committed `.example`, and its canonical form must be reachable from a public repo. If
the canonical registry is operator-private, ISMA consumes a **published projection** of it, not the
private artifact — and the gate refuses when the projection is absent rather than assuming admit.

---

## 7. Deliberately out of scope

- **Content/PII scanning** — §1.
- **Retroactive reach.** The ~1,954 files checkpointed before 2026-07-09, when the parser worked, are
  a realized historical exposure. No forward gate addresses them; that is a separate disposition.
- **Consumers outside isma-core.** Nothing in this repo reads `PARSED_DIR`. Whether anything outside
  it does is **Unknown** and is not closable from isma-core tooling. This gate governs the ISMA
  corpus write path and claims nothing beyond it.
- **The training-side registry itself.** tutor is custodian. ISMA is a consumer, not the owner.

---

## 8. Open — needed from tutor before implementation

1. Canonical location and schema of the exclusion/hold registry, and its published-projection form.
2. How lineage ancestry is expressed — parent session ID per record, or a separate lineage edge set?
3. Whether a `HELD` record means *refuse* or *refuse-and-alert*; they need different call sites.
4. Versioning: how a consumer detects it read a stale policy.

## 9. How this gets verified

Not by tests I author — per repo discipline, production is the oracle. The gate is proven by: a
refusal observed on an unknown source; an admit observed on an explicitly-admitted corpus root with
its recorded reason; a deliberately corrupted policy file producing a refusal rather than an admit;
and the existing prose ingest continuing to work under an explicit admit rule. Those four, observed
in production, are the evidence — not a green unit and not a passing suite.
