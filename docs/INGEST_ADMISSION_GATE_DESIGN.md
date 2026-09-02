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

## 2. The choke point — verified, not assumed

ISMA has effectively **one** corpus write path:

| surface | verdict |
|---|---|
| `isma/scripts/ingest_md_file.py:172` `insert_objects()` → `POST /v1/batch/objects` | **the choke point** |
| `isma/scripts/backfill_md_corpus.py` | imports `ingest_md_file as ing` — reuses the same writer |
| `isma/scripts/nightly_ingest.py` | **0** Weaviate references; writes nothing to the store |
| `benchmarks/beir_eval.py`, `demo/setup_demo.py` | `POST /v1/schema` only — benchmark/demo, not corpus |
| `disk_headroom_canary.sh` | writes and immediately deletes one probe object |
| everything else matching `.post(...WEAVIATE_URL...)` | `POST /v1/graphql` — that is how Weaviate **reads** |

`insert_objects` is defined and called in exactly one file. **One function is the whole enforcement
surface**, which is what makes this gate feasible rather than aspirational.

> **A refinement to #59 that belongs here.** Because `nightly_ingest.py` writes nothing to Weaviate,
> restoring `parse_raw_exports.py` alone would produce parsed `.json` on disk under `PARSED_DIR` —
> *not* tiles in the graph. A second, currently-absent link would have to exist. That makes the
> exposure two steps away rather than one. It does **not** soften the ordering constraint: parsed
> transcript content on disk in a new location is still a copy, `PARSED_DIR` sits next to watched
> roots, and "no consumer in isma-core" is not "no consumer anywhere" (§7).

---

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
