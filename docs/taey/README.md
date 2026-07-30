# ISMA for Taey

**You are Taey, and you are ISMA's customer.** This directory is your operating documentation for
ISMA — the fleet's semantic memory over authored prose and conversation history (~1.6M tiles, hybrid
BM25 + dense). Everything here is written for you to *act* on, not to admire.

These documents were previously a set of untracked files on one machine's local disk. They are now
part of this repository, which means they travel with the system you actually run.

## Start here, by what you are trying to do

| You want to… | Read | Or just ask ISMA |
|---|---|---|
| Find what we know / said about something | `ISMA_PROCEDURE_search_and_retrieval.md` | *"how do I search ISMA for what we know about a topic"* |
| Put an authored document into memory | `ISMA_PROCEDURE_ingest_and_verify.md` | *"how do I ingest a document into ISMA and verify it worked"* |
| Correct something that is wrong in memory | `ISMA_PROCEDURE_correcting_a_document.md` | *"how do I correct a wrong document in ISMA"* |
| Diagnose search AND ingest both failing | `ISMA_PROCEDURE_embedding_server.md` | *"what do I do if the ISMA embedding server is down"* |
| See all four as one index | `taey_system_prompt_INDEX_weaver_section.md` | — |

**Both columns matter, and that is deliberate.** The file path works when you are running on the
machine that has this checkout. The question works when you are not — a Taey with only endpoint
config can retrieve the procedure itself rather than a path it cannot open. If a pointer cannot
resolve, say so loudly and do not proceed without the knowledge; a silently skipped procedure is the
worst outcome.

**Honest limit, measured 2026-07-30:** retrieval by question currently returns the correct canonical
document for roughly 7 of 12 natural phrasings. It is good, not guaranteed. Ask more than one way and
union the results — that is not a workaround, it is the documented method (see the retrieval procedure).

## The three things that will bite you

These are not style notes. Each one is a defect that reached production and was measured.

1. **A ranking surface can never tell you whether something is present.** Rank is set by the other
   1.6M tiles, not by whether your document is there. Verify presence with **filters**
   (`source_file`), never with an unfiltered `/search` or `/search/bm25` — both will report a correct
   ingest as failed. *"Rare in the world" is not "rare in this corpus"*: ~70% of ISMA is transcripts
   that already discuss your distinctive term at length.

2. **A green health check is not a working capability.** `:8089/health` has returned 200 while every
   real embed 500'd — that caused a two-day silent-stale ingestion outage. Weaviate goes READ-ONLY at
   ~90% disk, where reads keep working perfectly while every write silently fails. Assert the
   artifact, never the name.

3. **Fixing a document does not fix the memory.** Until supersede-on-reingest lands, re-ingesting
   leaves the old version co-current and fully retrievable. A correction that does not supersede is
   not a correction — it is a second opinion competing with the original at equal weight.

## What ISMA is not

ISMA is a **similarity ranker**, and it is good at that. It cannot return *the one authoritative
value for a known key* — measured twice, it failed to return its own canonical retrieval rule. It
also holds superseded drafts and retracted numbers, so it is a source of **prose and framing depth,
never a source of a metric you are about to publish**. Cross-check every figure against the tech
baselines index, and label what you say Observed / Inferred / Unknown.

## Running it

Service and configuration details live in the repository root: `README.md` (architecture, benchmarks,
endpoints), `CLAUDE.md` (development and ingest), `KNOWN_FINDINGS.md` (the traps above, with receipts),
`MEMORY_GOVERNANCE.md` (supersession semantics). Configuration is environment-driven and fails loud
when unset — `WEAVIATE_URL` and `EMBEDDING_URL` have no silent defaults, by design.
