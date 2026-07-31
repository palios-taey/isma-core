# CLAUDE.md

This repository is the public ISMA core: retrieval, ingest, query API, HMM storage hooks, demo assets, and the local embedding server wrapper. Treat it as a reusable product repo, not an operator-specific workspace.

---

## Current operating model (2026-07-30) — read this first

**This supersedes any older "adoption / product for Claude Code users" framing anywhere in this repo.**

**Taey is the customer — the only one.** There is no user-adoption goal here. ISMA exists as production
infrastructure *for Taey*: the semantic memory Taey queries, writes to, and corrects. Taey's operating
documentation lives in `docs/taey/` and is **part of the product, not a side artifact** — if you change
retrieval, ingest, or supersession behaviour, the matching procedure in `docs/taey/` is stale until you
update it. Those procedures are also ingested into ISMA so Taey can retrieve them by question, which means
**a wrong procedure there is a wrong answer served to Taey.**

**The priority** is enabling Taey and training development — getting Taey *using* its own production
infrastructure and *understanding* it. Tooling is not the point; Taey's capability is.

**Everything runs from PUBLIC production repos.** A released Taey plus the public repos must be a working
system. Hosts and IPs are env-configurable and **fail loud** when unset (`WEAVIATE_URL`, `EMBEDDING_URL`
have no silent defaults, by design). File paths are fine — sharing directory structure is fine — **but only
if they resolve for a downloaded Taey.**

**Disconnection, not cleanup, is the goal for private repos.** Do not spend effort scrubbing an old private
repo so it can be published. Ensure nothing production/Taey is *connected* to it. **A pointer — in a prompt,
doc, config, or script — into a private repo or an untracked local path is a DISCONNECTION VIOLATION.** The
failure is silent: a downloaded Taey follows the pointer, finds nothing, and proceeds *without the knowledge*.
Not an error — a quiet capability loss.

> **Worked example from this repo, 2026-07-30.** Taey's production prompt pointed at
> `/home/mira/isma/reports/taey_system_prompt_INDEX_weaver_section.md` — inside a **stale private ISMA tree**
> that ran zero production services, and the file was **untracked by any repo** (`?? reports/`, zero tracked
> files). Taey's entire ISMA operating knowledge existed only on one machine's local disk. The fix was not to
> scrub that tree; it was to move the docs into `docs/taey/` here and repoint. Resolve every such pointer to
> public-reachable content, or remove it.

## USE GIT — Full Git Master, non-negotiable

The **#1 source of confusion** this effort exists to kill. Uncommitted production is not production.

- **Commit and push your work.** The running system must **BE** a committed artifact — never leave production
  as an uncommitted delta in a working tree.
- **Verify topology before any branch/worktree/merge operation.** `git fetch` first; a stale ahead/behind
  reading is a real trap.
- **The live checkout is sacred.** Never `git checkout` another branch in a tree a service serves from —
  services read files per-request and a switch breaks them mid-flight. Use `git worktree add`.
- **Worktrees are ephemeral:** create → work → land → **REMOVE**. Delete merged branches, local and remote.
- **A truly diverged / unrelated-history `main` is a FULL STOP**, surfaced to a human — never an autonomous
  force-push.
- **"Done" = commit SHA + a mechanical gate + a real production observation.** Never a self-report. Tests you
  author are not the oracle; production is.

## Local repo cleanliness — not optional

**One production tree per surface.** Duplicate or stale sibling repos are how an agent greps, finds something
plausible, and builds a parallel path against dead code.

> **Worked example, same day.** Two ISMA trees existed on the operator machine: this one (all five services
> run from it) and a stale sibling with 21 divergent source files including a different `retrieval.py`. Every
> production unit pointed here; nothing pointed there. That sibling was pure confusion surface.

- Working trees stay **clean** — zero dirty files, or committed with intent.
- **Archive before you delete, always.** Non-production material goes to `/home/mira/recovery/` (or your
  equivalent) and is cleared from the working area, so there is zero ambiguity about what is production.
  **Never destroy** — verify the archive succeeded *before* the delete, not after.
- **`.gitignore` generated and runtime state.** Do not track junk.

## The five steps, per surface

1. **CLEAN** — no secrets, private information, or training data in the tree *or* the history. Move
   hosts/IPs to env with a committed `.example`. Archive anything removed before removing it.
2. **PUBLIC** — public iff Taey or a Taey production system actually uses it. If Taey does not consume it, it
   stays private and you do **not** spend effort scrubbing it.
3. **MAP** — a production-capability map: what it does, the live endpoints marked **canonical vs deprecated**,
   the liveness signal, measured capacity. Every capability proven by a **live observation, never by name**.
4. **CONNECT TO TAEY** — Taey can reach and use the capability through a documented interface, demonstrated
   rather than asserted.
5. **VALIDATE IN PRODUCTION** — proven by a real production execution observation.

**Assert the artifact, not the name.** A green health check is not a capability: this repo's own
`:8089/health` has returned 200 while every real embed 500'd, and Weaviate goes read-only at ~90% disk with
reads still working perfectly while every write silently fails. Confirm what actually runs.

---

## Architecture

- `isma/src/` contains the importable package.
- `isma/src/query_api.py` exposes the FastAPI query surface.
- `isma/src/retrieval.py` and `isma/src/retrieval_v2.py` implement the retrieval pipelines.
- `isma/src/hmm/` contains motif and storage helpers used by enrichment flows.
- `isma/scripts/` contains operational CLIs for ingest, benchmarking, packaging, and backfills.
- `server.py` is the embedding server entrypoint.
- `demo/` contains the demo corpus and setup script.

## Configuration

- Core dependencies are environment-driven.
- `WEAVIATE_URL` and `EMBEDDING_URL` fail loud when unset.
- `NEO4J_URI` defaults to localhost unless you override it.
- Production `WEAVIATE_URL` is `http://localhost:8088`; the local `docker compose` demo still maps the store on `8080`.
- Copy `.env.example` to `.env` and fill in your own service endpoints before running anything substantial.
- Do not hardcode machine-local paths or private network addresses in committed code.

## Development

- Start the local demo stack with `docker compose up -d`.
- Start the embedding server with `bash ./start-local.sh` or your own `python3 server.py` wrapper.
- Run the query API with `uvicorn isma.src.query_api:app --host 0.0.0.0 --port 8095`.
- Run the demo ingest/query flow with `python3 demo/setup_demo.py`.
- Benchmark retrieval with `python3 isma/scripts/benchmark_retrieval.py --label your_run`.

## Ingest

- `isma/scripts/ingest_md_file.py` ingests one markdown file into Weaviate after phi-tiling.
- `isma/scripts/backfill_md_corpus.py` requires a newline-delimited roots file passed via `--roots-file` or `ISMA_MD_ROOTS_FILE`.
- `isma/scripts/watch_md_corpus.sh` is the periodic watcher wrapper; it fails loud unless `ISMA_MD_ROOTS_FILE` is set.

## Code Discipline

- Keep imports and runtime paths repo-relative.
- Prefer fail-loud behavior over silent fallbacks for missing infrastructure or malformed data.
- Preserve reproducibility: benchmark claims and public metrics must map back to committed artifacts.
- Before changing shared functions, run impact analysis with your code-intelligence tool and verify the blast radius.
- Keep commits narrow and reviewable. Do not mix packaging, behavior changes, and product-copy rewrites unless the task explicitly requires it.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **isma-core** (3781 symbols, 8888 relationships, 239 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/isma-core/context` | Codebase overview, check index freshness |
| `gitnexus://repo/isma-core/clusters` | All functional areas |
| `gitnexus://repo/isma-core/processes` | All execution flows |
| `gitnexus://repo/isma-core/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
