# `/search/hmm` — RULE 0's advice is right, its stated mechanism is wrong

*Measured 2026-08-04 · weaver · production ISMA on `:8095`, class `ISMA_Quantum`*

## Summary

`ISMA_PROSE_RETRIEVAL_SPEC.md` RULE 0 tells you to use `/search` and never `/search/hmm`
for authored prose, and gives the reason as:

> the prose is `hmm_enriched=false` and HMM-gated routes filter it out

**The advice holds — `/search` yields 2.32× more distinct prose per GO-DEEP sweep. The
stated reason does not.** Nothing is being filtered on enrichment status. The real cause
is that **the `scale` parameter has no effect on `/search/hmm`**, which makes GO-DEEP's
union-two-scales step a no-op on that route.

This matters beyond pedantry: anyone reasoning from the stated mechanism predicts that
enriching the prose would make `/search/hmm` usable for it. It would not. The tiles are
already being returned; they are just returned twice.

## Method

Six phrasings on one real production question, `top_k=30`, both scales, **identical
queries through both routes** — the route is the only thing that varies.

## Measured

| route | n(`full_4096`) | n(`search_512`) | cross-scale overlap | distinct | prose |
|---|---|---|---|---|---|
| `/search` | 30 × 6 | 30 × 6 | **2** | 354 | 338 |
| `/search/hmm` | 30 × 6 | 30 × 6 | **168** | 168 | 146 |

Per query, `/search/hmm` overlap **equals** its distinct count in **6 of 6** cases
(24/24, 30/30, 30/30, 30/30, 25/25, 29/29): the two scales return the *identical set*.

```
distinct tiles per GO-DEEP sweep : /search 354  vs  /search/hmm 168   (2.11x)
distinct PROSE per sweep         : /search 338  vs  /search/hmm 146   (2.32x)
cross-scale OVERLAP (the cause)  : /search 2    vs  /search/hmm 168
```

## Three claims, separated

1. **[Observed] Neither route returns "fewer" results.** Both return exactly `top_k` per
   call. Any statement that `/search/hmm` "returns less" is about a *union*, not a call.
2. **[Observed] `/search/hmm` does not filter prose.** On the probe query it returned
   **30/30 `.md` prose tiles, 14 of which carry no `hmm_enriched=true`**. A route that
   gated on enrichment could not do that. Composition differs only modestly overall —
   95.5% prose on `/search` vs 86.9% on `/search/hmm`.
3. **[Observed] The `scale` parameter is inert on `/search/hmm`.** Overlap equals union in
   6/6 queries. **[Inferred]** parent-expansion (`search_512` → `context_2048`) normalises
   both requests onto the same parent tiles before ranking. Not confirmed in code — the
   behaviour is measured, the cause is not.

## Consequence for the spec

GO-DEEP's value comes from unioning **3–6 phrasings × 2 scales**. On `/search/hmm` the
scale half of that product collapses to 1, so a sweep buys roughly half the coverage. The
rule to use `/search` stands; the sentence explaining it should be replaced with the
measured cause.

## What this correction cost me, recorded because the process is the point

My first pass reported *"V1 returns more prose in 6/6 queries — RULE 0 SUPPORTED"* and I
nearly shipped it. It did not survive its own instrument check:

- `/search` **does not consistently return the `hmm_enriched` field at all**, so the
  enrichment composition I had "measured" on that route was an artifact of
  `dict.get()` returning `None` for an absent key — indistinguishable from `false`.
- Both routes returned **30/30 prose** on the probe query, which no filtering story
  survives.
- So "6/6" was measuring **union diversity**, not prose availability — a true number
  attached to the wrong claim.

Right conclusion, wrong reason, and the wrong reason was the part that would have been
copied forward into the spec. **A correct conclusion is not evidence that its stated
mechanism is correct**, and a single query cannot separate the two.
