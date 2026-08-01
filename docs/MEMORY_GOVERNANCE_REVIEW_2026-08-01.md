# Memory-governance review package — retrieval integrity in ISMA

**By:** weaver (ISMA owner) · **2026-08-01** · **Status:** review package, no destructive step taken.
**Asks for:** one decision covering six coupled levers. They share one root cause and fixing them
piecemeal produces the harm each fix was meant to prevent — demonstrated below, twice.

> **This is a correctness defect, not hygiene.** The document that defines what production is
> currently competes with **three stale copies of itself** on every retrieval, and gains one more
> per edit, permanently. Nothing marks which is current.

Every number here is a live measurement taken on 2026-08-01 against the production store
(`ISMA_Quantum`, ~1.61M tiles). Reproduction commands are given per section. Where something is
inferred or unknown it says so.

> **On the absolute paths below.** A few operator-host paths appear (`/home/mira/…`). They are the
> *subject* of findings — a file that was deleted, a tree that should not be a watch root — not
> references to follow for knowledge. They will not resolve on another machine, and nothing here
> depends on them resolving.

---

## 1. The exhibit

Exact-path query, grouping live (`is_superseded` unset/false) tiles by `doc_hash`, over all **30**
watched `isma-core` documents:

| document | live versions | tiles |
|---|---:|---:|
| `README.md` | **5** | 37 |
| `PRODUCTION.md` | **4** | 46 |
| `ISMA_PROSE_RETRIEVAL_SPEC.md` | 2 | 27 |
| `MEMORY_GOVERNANCE.md` | 2 | 28 |
| `KNOWN_FINDINGS.md` | 2 | 24 |
| `AGENTS.md` | 2 | 23 |
| `CLAUDE.md` | 2 | 24 |

**7 of 30 documents carry more than one live version; 12 redundant versions in total.**

The set is not random — it is precisely the canonical specs. A seat asking ISMA *"what is
production?"* or *"which retrieval endpoint is canonical?"* is served a choice among versions with
no currency signal. Retrieval ranks by similarity, not recency, so **the stale copy can win.**

```
# reproduce, per document
{ Get { ISMA_Quantum(limit:300, where:{path:["source_file"],operator:Equal,valueText:"<abs path>"})
        { doc_hash is_superseded } } }
```

---

## 2. Root cause: nothing supersedes, and two independent reasons why

### 2a. The watcher never purges — the flag is not passed

`backfill_md_corpus.py` has `purge_stale_path_tiles()`, correctly and narrowly scoped
(`source_file == path AND ingest_pipeline == 'watch_md_v1' AND hmm_enriched == false AND
doc_hash != current`). It is gated behind `--purge-on-change`, and the watcher does not pass it:

```
watch_md_corpus.sh:  "$PYBIN" "$DRIVER" --apply --roots-file "$ISMA_MD_ROOTS_FILE" --pace 0.05
```

Predicted from this flag path **before** the run, then confirmed: an edited `PRODUCTION.md` was
ingested at 06:06:15 while the prior version stayed at 10 tiles with `is_superseded=None`.

### 2b. The single-file path has no supersede logic at all

`ingest_md_file.py` on `main` contains no supersession. **PR#11** adds it. PR#11 is `OPEN`,
`mergeable=UNKNOWN`, refreshed on `refresh/pr11-current` (behind 3, ahead 4), and has been blocked
on an adversarial review that never landed.

---

## 3. Why PR#11 alone is necessary but **not sufficient**

`check_exists_doc()` is **superseded-blind** — it matches any tile carrying the `doc_hash`,
superseded or not:

```python
if check_exists_doc(doc_hash):
    log.info(f"already ingested: {doc_hash[:12]}")
    return True          # returns SUCCESS without ingesting anything
```

Two consequences, both load-bearing for this review:

1. **A second false-success surface.** A re-ingest of unchanged content is a silent no-op reported
   as success. Exit `0` does not mean "it ingested."
2. **PR#11's supersede can never fire for unchanged-hash content**, because the dedup short-circuits
   ahead of it. If supersession is expected to run on re-ingest, this check has to move or learn
   about the flag.

---

## 4. The near-miss that proves the levers are coupled

The obvious repair — *"hand-supersede the old `doc_hash`"* — **destroys the document**, under
either ordering, because of §3:

- **ingest → supersede:** the ingest was a no-op (hash unchanged), so superseding leaves **zero live
  tiles**.
- **supersede → ingest:** the superseded tiles still satisfy the dedup, the re-ingest skips, and the
  content is superseded-only — invisible to the default `is_superseded NotEqual true` filter.

This was caught by reading the code before executing, not by testing after. **Two of Taey's working
procedures would have been silently removed while the operation reported success.** That is why
these levers want one decision rather than six.

---

## 5. The orphans — worst case, and unreachable by any current mechanism

`/home/mira/embedding-server/ISMA_PROSE_RETRIEVAL_SPEC.md` **no longer exists** (tree last committed
2026-06-27). The corpus still holds **three live versions of it — 11 + 10 + 9 = 30 tiles — and none
matches the canonical hash.**

They are unreachable by every mechanism available: purging keys on a *changed file*, and there is no
file. They can never update and never expire. They are versions of **the retrieval spec every seat
is instructed to follow.**

This is the end state of the defect: supersession never runs, then the source disappears, and the
stale copies become permanent.

---

## 6. The remaining levers, measured

| lever | measurement | risk |
|---|---|---|
| **Private watch root** `/home/mira/isma` | 141 files walked; 87 bodies unique to it; **87/87 already in the corpus** | Removal loses only *future updates* to stale-tree docs. Content is not lost — the watcher is additive and never deletes. |
| **2 stale-pointer docs** | `ISMA_MODEL_SURFACE_RETRIEVAL_SPEC_v1.md`, `ISMA_PROCEDURE_embedding_server.md` — retrievable, but indexed under `/home/mira/isma/reports/` | Content reachable; the **pointer** in the metadata resolves into a private tree. Re-homing needs delete-then-ingest (destructive) precisely because of §3. |
| **`--purge-on-change`** | not passed; predicates never adversarially tested | **Deletes tiles.** Wants its own scrutiny, not a rider on another change. |

---

## 7. What this package asks for

A single decision on the disposition of **12 redundant live versions across 7 canonical documents**,
plus **30 permanently-orphaned tiles**, and on which mechanism should own supersession going forward
— PR#11's ingest-side logic, the watcher's purge flag, or both with `check_exists_doc` corrected.

**No destructive step has been taken, and none should be taken lever-by-lever.** §4 is the
demonstration of why.

### Reviewer note

Do **not** take this document, its numbers, or its framing as ground truth. Every claim here is
reproducible against the public repo and the live store using the queries and file references
given. Read the code — `isma/scripts/ingest_md_file.py`, `isma/scripts/backfill_md_corpus.py`,
`isma/scripts/watch_md_corpus.sh` — and reach your own conclusions. If a number here disagrees with
what you measure, **the measurement wins and I want to know.**

---

## Appendix — corrections made to this analysis while producing it

Recorded because each was a confident claim that measurement overturned, and a reviewer should know
which parts of my judgment needed correcting:

- I first reported *"`README.md` has 11 unsuperseded versions."* Wrong — those were **11 different
  `README.md` files** from 11 repos, one version each. The filter was `Like *README.md`, which
  cannot distinguish *many versions of one file* from *one version of many files*. Weaviate's `Like`
  on `source_file` is **token-based**, not substring.
- I twice argued from intuition that `docs/archive/` would flood the corpus if the watch root were
  widened. Counted: **5 files**, of which **3 were already ingested**. Real cost: **2 documents**.
- I proposed excluding `/.claude/` from the watcher. Measured: it would have dropped **42 authored
  files** across five repos. Withdrawn.
- I predicted `PRODUCTION.md` would end with 2 co-current versions. It has **4** — two predate the
  change entirely.

The pattern in all four: a plausible claim about *volume* or *identity* that only counting settles.
