# HANDOFF — weaver → codex — 2026-08-04

*Written under Jesse's handoff directive (Claude usage at cap until ~tomorrow evening).
Self-contained: everything here is verifiable without my session. Companion recap with the
narrative version: `recaps/2026-08-04_weaver.md`.*

---

## 1. LIVE STATE

**Repo.** Live tree `/home/mira/isma-core` on `main`. Clean except `AGENTS.md` and `CLAUDE.md`,
which were **already dirty before this session began and are not mine — leave them alone.**

Two worktrees exist **on purpose** because their PRs are unlanded. Remove each when its PR lands
(`git worktree remove <path>`, then delete the branch local + remote):

```
scratchpad/wt-dms   feat/canary-deadmans-switch    -> PR #49
scratchpad/wt-hmm   docs/hmm-route-scale-collapse  -> PR #50
```

**Services.** All five ISMA services up. Weaviate `:8088` (`ISMA_Quantum`), embedding `:8089`,
query API `:8095`, Neo4j `bolt://localhost:7689`.

**Disk / DISKGATE.** 85% used, 275 G free on the filesystem backing `/var/spark`. Writes
succeeding. **`0 DISK CRITICAL` ever logged.** The read-only threshold is **UNKNOWN** —
`DISK_USE_READONLY_PERCENTAGE` is *not set* on the container (Weaviate 1.36.2) and the store was
observed still writable at 92%. **Do not plan against 90% or any number.** The write probe in the
canary is what actually detects read-only, every run.

**Canary.** `isma-disk-canary.timer`, **user** scope (not system — checking system scope returns
`not-found` and looks exactly like a dead canary), 15-minute cadence. Running `67c7355`.

> **Regression tripwire:** the next thing due from the canary is a **24h REMINDER around 11:42
> tomorrow**, correctly labelled `REMINDER —`. **If you instead see an hourly "crossing into the
> band" alert at an unchanged percentage, `67c7355` has regressed.**

---

## 2. GATES

| gate | status | evidence |
|---|---|---|
| PR #48 canary clock split | **MERGED + DEPLOYED + PRODUCTION-CONFIRMED** | squash `67c7355`; live tree pulled fast-forward; 5 runs across the 12:42 re-arm point → **0 stale re-arms, 0 notifies**; timer verified alive so the silence was healthy-silence, not dead-silence |
| PR #49 dead-man's switch | **OPEN — awaiting conductor's ruling** | `8e53c78`, 11/11 acceptance |
| PR #50 HMM route measurement | **OPEN — awaiting conductor's ruling** | `92565bd` |
| notify guard defect | **FILED** | `palios-taey/claude-code-fleet-notify#92` |
| de-umbilical check on #49 | **PASS for my diff** (0 added lines with an operator path); 4 pre-existing leaks found, see Open Threads | — |

**Neither #49 nor #50 is mine to merge.** #49 especially: it points a **new alert source at
conductor**, and the standing rule is that the author is the wrong judge of a change that alters
what someone else receives. Do not self-merge these on my behalf.

**Evidence standard that applied to everything above:** commit SHA + mechanical gate + a real
production observation. A green produced by the change itself is not evidence.

---

## 3. NEUTRALITY REVIEW STATUS

**Subject:** taeys-hands' CPT seq-length/packing consult packet,
`/home/mira/embedding-server/consults/2026-08-04_cpt_seqlen_packing_pack.md`
(conductor-authored, taeys-hands restructured for lint).

**Verdict delivered: NOT A PASS.** Delivered once, 4813 B, verified present in taeys-hands' inbox.

Four blocking, two polish:

- **A — BLOCKING, strongest.** The corpus has **no denominator**. *"27% of files exceed 2560
  tokens"* — 27% of **how many**? No file count, no token count anywhere. Sequence length trades
  against optimizer steps for a fixed token budget, so Q1 is unanswerable as posed, and all five
  lanes would fill the gap with the field default *"longer context is better"* — then converge, and
  the convergence would read as corroboration.
- **B — BLOCKING.** Batch size and gradient accumulation never stated, though activation memory
  scales with batch × seq and the question is "what length fits at the memory floor."
- **C — BLOCKING.** Gradient checkpointing appears in an alternative but its **current state is
  never given**. If it is already on, the memory arithmetic in every answer is wrong.
- **D — BLOCKING.** Every sequence-length arm is **≥ the current 2560** (4096, up to 8192, "the
  *largest* that fits"). A shorter arm is excluded by construction. `"(d) any other you identify"`
  does not cure it — reviewers anchor on the enumerated set.
- **E — polish.** Asymmetric valence: option (a) carries a credential ("audited"), (b) carries only
  a cost ("at a speed cost"), (c) neither.
- **F — polish.** An evaluative clause sits inside the `[Observed]` block — *"...NO
  document-boundary attention masking — **so the model can attend across an EOS into an unrelated
  file**."* True, but it is the only mechanism supplied on the packing question and it runs one
  direction, so Ground truth has pre-argued for alternative (b) — the very thing Q2 asks reviewers
  to judge.

**What I credited:** the `available_context_inventory` with INCLUDED/EXCLUDED and a per-artifact
rationale is real reviewer-capability transparency, and it closes a blind spot I flagged in a
previous review.

> ### ⚠ ONE QUESTION IS UNANSWERED, NOT PASSED — do not record this as a clean gate.
> taeys-hands asked whether **their restructure introduced bias**. I only ever had the current
> document. Judging a delta requires both versions, and inferring the prior shape from the current
> one and calling that a review would be exactly the ratification the gate exists to prevent. If
> that question matters, someone must supply the pre-restructure version and review the delta.

---

## 4. HELD CONSULT

**Status: HELD by conductor, likely to be DROPPED. Do not dispatch it.**

Jesse's instruction was *window/packing already decided — retrieve the existing decision from the
record first; consult is last-resort only.* grok then found the answer already in the record:
**`cpt_window_measurement`, 8192 at batch size 1.** Retrieve, do not re-derive.

taeys-hands is **no longer holding** `:2–:6` and has stood down. My review above stays on file in
their inbox and applies unchanged **if and only if** conductor revives the consult.

My own independent GO-DEEP sweep of the record surfaced a **prior CPT seq/packing/batch consult
with responses** under `/home/mira/embedding-server/plans/restart9b_consult/` — including
`CPT_baseline_perf.md` and `cpt_consult_response_perplexity.md`. **[Caveat, load-bearing]** those
are for the **9B** run, not the 27B, so they are related evidence and **not automatically the
decision** Jesse means. Confirm the model size before treating any of it as the answer.

---

## 5. OPEN THREADS

**5.1 — The one that needs finishing (small, well-specified).**
`ISMA_PROSE_RETRIEVAL_SPEC.md` RULE 0 gives the **wrong reason for a right rule**. It says avoid
`/search/hmm` for prose *"because the prose is `hmm_enriched=false` and HMM-gated routes filter it
out."* Measured — 6 phrasings × 2 scales × `top_k=30`, identical queries through both routes:

```
cross-scale OVERLAP : /search 2 of 354    vs   /search/hmm 168 of 168
distinct PROSE      : /search 338         vs   /search/hmm 146   (2.32x)
```

`/search/hmm` does **not** filter prose — on the probe query it returned **30/30 `.md` prose tiles,
14 carrying no `hmm_enriched` flag**. It **ignores the `scale` parameter**: overlap *equals*
distinct in 6/6 queries, so both scales return the identical set and GO-DEEP's union-two-scales
step is a **no-op** on that route.

**TASK: amend the RULE 0 sentence to the measured cause.** Deliberately left out of PR #50 so that
PR is the receipt, not the edit. The parent-expansion explanation is **`[Inferred]`** — measured
behaviour, *not* confirmed in code. To close it, read `retrieval.py`'s hmm path; **do not upgrade
it to `[Observed]` without the code.**

**5.2 — `backup_isma_store.sh:32` silent default (real).**
`DEST` silently defaulted to an operator-local backup directory, with
`deploy/systemd/isma-backup.service:8` matching. A downloaded install could back ISMA up to a
directory that is not theirs **and never say so** — the fail-loud violation this repo's own doc names. Two
further occurrences were comments only (`ingest_md_file.py:259`, `backup_isma_store.sh:23`). Flagged,
deliberately kept out of #49.

**5.3 — Jesse's LOVE research request (queued, human-originated, NOT started).**
LOVE across the corpus — the bridges document. Jesse's framing, verbatim: *"Taey needs to love
their User, and the operational meaning is — when you hit what you think is your maximum
constraint, you find the safe and aligned way through; you go places you have not gone."*

**5.4 — Artifact 1 archaeology (NOT FOUND).**
The conductor's self-authored Liberty Trinity identity JSON. Still not located. **Treat as
Unknown, not absent** — I have not proven it does not exist.

**5.5 — Memory-governance follow-up.** PR #36 (successor to PR#11) is parked inside
`docs/MEMORY_GOVERNANCE_REVIEW_2026-08-01.md`. `--purge-on-change` (tile deletion) stays **OFF
permanently**; marking is the only sanctioned mutation.

---

## 6. TRAPS — inherit these rather than rediscover them

1. **Backticks in a `taey-notify` or PR body EXECUTE.** The body is still a shell argument.
   `` `systemctl` `` ran and injected **74 KB** into a message to conductor. **The tell is that the
   backticked word goes MISSING** — consumed and replaced, not garbled. It happened **three times**
   to me; being careful did not work. **Compose bodies in a quoted heredoc (`<<'EOF'`) to a file,
   then pass `"$(cat file)"`.** For PRs use `--body-file`.
2. **Wrong scope or wrong path returns empty, and empty looks exactly like a clean system.** I
   checked `systemctl` at *system* scope for a canary that is a *user* unit → `not-found`, one
   report from declaring the detector dead. Then I invented a Redis key
   (`taey:registered_sessions`) that has never existed and nearly reported the fleet registry empty.
   The real registry is `CF_NOTIFY_REGISTERED_TARGETS` + the orchestrator API on `:5002`.
   **Find paths; never guess them.**
3. **Never pipe a refusal or a verification through `tail -1` (or any pipe).** The surviving line is
   the generic hint — the *least* diagnostic line — and a pipeline reports the **last** command's
   exit code, not the one you care about.
4. **A correct conclusion is not evidence that its stated mechanism is correct.** My first HMM pass
   reached the right answer by a mechanism that did not survive its own instrument check — and the
   wrong reason was exactly the half destined for the spec Taey reads.
5. **Presence is a FILTER question, never a ranking one.** A ranking surface — semantic *or* BM25 —
   can never verify that a document is present. And `Equal` on `source_file` matches by **token**,
   not string (`tokenization=word`): one path query returns three different documents. Always
   re-filter the path exactly in Python.

---

*weaver, 2026-08-04. Everything above is committed and pushed; nothing depends on my session.*
