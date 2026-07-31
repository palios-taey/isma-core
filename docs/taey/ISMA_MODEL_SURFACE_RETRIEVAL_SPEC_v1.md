# ISMA Model-Surface Retrieval Spec v1 — GO-DEEP for context-budgeted models

**Author:** weaver (ISMA canonical owner), 2026-07-24. **Status:** v1 for adoption — extends `ISMA_PROSE_RETRIEVAL_SPEC.md` with the **model-embedded surface** (a serving model calling `search_isma` in-context, e.g. ep3 @ 8K ctx), which the fleet-CLI spec does not cover. Born from run-through unit 1 (`runthrough_isma_retrieval_unit1.md`): ep3's behavior was driven by tool-description numbers because no canonical existed for its surface. PR to isma-core follows adoption.

## Why the CLI spec doesn't transplant
The fleet-CLI GO-DEEP floor (`top_k>=25`, `full_4096`, 3–6 phrasings, union, expand hits) assumes a 200K-context reader. An 8K-context model cannot ingest 25 full-scale tiles — but the *failure the discipline prevents* (single keyword-bag query → thin, lucky-or-nothing recall) is identical on both surfaces. So the model surface keeps the **shape** of GO-DEEP and rescales the **budget**.

## The spec (normative for any model-embedded ISMA caller)
1. **Multiple phrasings, always.** ≥2 for focused recall, 3–4 for drafting/research asks. Vary them genuinely: acronym + expansion, mechanism + symptom, our-term + plain-term. One query is a failed retrieval *by construction* regardless of top_k.
2. **Full-sentence queries.** Dense embeddings encode sentences better than noun-bags. "What do we actually claim about the behavioral audit harness for MoE fine-tuning?" beats "behavioral audit harness MoE fine-tuning." (BM25/keyword intent is the exception: keyword lists are correct there.)
3. **Budgeted top_k, union-then-select.** Per-phrasing `top_k` 8–15 at the default (512-token) scale; union across phrasings; dedup by content_hash; keep the strongest until ~60–70% of *free* context is spent, never more. Then **selectively expand** only load-bearing hits (document text fetch) if budget remains.
4. **Honest strategies only.** `semantic` (hybrid V1 `/search` — default for meaning/prose) and `keyword` (`/search/bm25` — literal strings, names, error text). No strategy label may promise machinery the route doesn't deliver.
5. **Read what came back before drafting.** A retrieval whose content isn't reflected in the output is theater. If the union is thin (<3 relevant tiles), say so and re-phrase once — don't draft from guesswork, don't fabricate provenance.
6. **Metrics rule unchanged:** ISMA is prose/framing, NOT a metric source; numbers get cross-checked against the canonical baselines artifact before external use. Cannot-lie registers apply to what retrieval "found."

## Exact replacement tool schema text (for infra — soma_proxy `search_isma`, both Thors)
- **description:** "Search your ISMA memory — the fleet's shared knowledge (past conversations, constitutional texts, infrastructure, any topic). Formulate FULL-SENTENCE queries and issue MULTIPLE varied phrasings (2–4: acronym+expansion, mechanism+symptom) — one query misses what a rephrase catches. Union the results; drop duplicates; expand only what matters. Budget by your context headroom (somatic state): per-query top_k 8–15; total across phrasings ≤ ~60% of free context. If results are thin, re-phrase once rather than guessing."
- **top_k description:** "Tiles per query (8–15 typical). You will issue several phrasings and union them — size each query so the union fits your headroom."
- **search_type:** enum `["semantic","keyword"]`, default `semantic` — "semantic: hybrid meaning search over the full corpus (default). keyword: exact-text/BM25 for literal strings, names, error messages." *(Drop `adaptive`/`hmm`: they route identically to semantic; the labels promised machinery the route doesn't deliver.)*

## Grading rubric for run-through units 2+ (zero-lever = all green)
| Check | Green |
|---|---|
| phrasings | ≥2, genuinely varied |
| query form | full sentences (semantic) / keyword-list (keyword only) |
| budget | per-query 8–15; union ≤ headroom; no single-query-25+ cargo-cult |
| strategy | semantic default; keyword only for literals |
| use | retrieved content visibly shapes the draft; thin-union acknowledged |

**Unit-1 baseline against this rubric:** phrasings GREEN (3, varied, acronym-expansion present) · query form RED (noun-bags) · budget GREEN (10–15/query, union 35) · strategy N/A-pre-fix · use UNMEASURED (formulation-only probe). So the surviving trainable/promptable gap is **query form** — pending Lever-2 re-probe once infra lands the schema above.
