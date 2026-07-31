#!/usr/bin/env python3
"""Generate docs/taey/ISMA_SCHEMA_REFERENCE.md from the LIVE system.

Taey cannot use ISMA correctly without knowing the tile schema and the API
request shapes. Hand-typing them guarantees drift: the class already carries 73
properties and no document enumerated any of them. So this generates the
reference from two authoritative sources at run time —

  * the live Weaviate class schema (property names + data types)
  * the FastAPI/pydantic request models in isma.src.query_api

— and refuses to emit anything it could not read. Regenerate after any schema
or API change; the doc carries the date it was generated so staleness is visible.

Usage:
    PYTHONPATH=$ISMA_HOME python3 isma/scripts/generate_schema_docs.py \
        [--weaviate http://localhost:8088] [--out docs/taey/ISMA_SCHEMA_REFERENCE.md]
"""
import argparse
import datetime
import json
import sys
import urllib.request

CLASS = "ISMA_Quantum"

# Properties Taey must understand to use ISMA correctly, with what they MEAN.
# Everything else is emitted as a plain name/type row; these get prose because
# misreading one of them produces a wrong answer rather than a missing field.
SEMANTICS = {
    "content": "The tile text itself. Read this, not `content_preview`, when mining.",
    "content_preview": "Truncated convenience copy. Never treat as the full text.",
    "source_file": "Absolute path of the document this tile came from. The ONLY reliable "
                   "way to verify a specific document is present — filter on it.",
    "doc_hash": "Hash identifying THIS VERSION of the source document. Changes on every "
                "edit. Two different doc_hash values for one source_file means two "
                "versions exist; exactly one should be non-superseded.",
    "content_hash": "Hash of the tile content; the key used by /document/{hash}/text.",
    "scale": "Chunk granularity: search_512 / context_2048 / full_4096 / rosetta. "
             "The same document is indexed at several scales.",
    "is_superseded": "TRUE = corrected/retired; default /search excludes these. The filter "
                     "is `NotEqual true`, so a tile with the flag UNSET is still returned "
                     "— unset is NOT hidden.",
    "superseded_by": "doc_hash of the version that replaced this one. Populated only for "
                     "recent writes and hand-stamped corrections, not the historical corpus.",
    "correction_status": "Why a tile was superseded (e.g. corrected). Provenance for the "
                         "refutation, not decoration.",
    "authority": "Explicit authority label. Ranking expresses RESEMBLANCE, never AUTHORITY, "
                 "so this is a FILTER concern like is_superseded. Currently unpopulated — "
                 "the labelling scheme is a deferred governance decision.",
    "hmm_enriched": "FALSE for authored prose. HMM-gated routes filter on this, which is why "
                    "they DROP the authored corpus — never use them for prose.",
    "rosetta_summary": "AI-generated summary of the tile. A compression: the source tile "
                       "remains deeper ground truth.",
    "epistemic_type": "operational_verified / inferred_pattern / symbolic_framework / "
                      "aspirational_design / open_question / superseded.",
    "truth_tier": "Truth-tier label. Currently unpopulated (deferred scheme).",
    "memory_zone": "Memory-zone label. Currently unpopulated (deferred scheme).",
    "lineage_root": "Root of this tile's derivation chain.",
    "parent_tile_id": "Parent tile for small-to-big (parent) expansion.",
    "session_id": "Conversation session this tile came from, for transcript sources.",
    "ingest_pipeline": "Which ingest path wrote the tile.",
    "source_type": "document / transcript / etc. ~70% of the corpus is transcripts.",
}


def fetch_schema(weaviate):
    url = f"{weaviate}/v1/schema/{CLASS}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def request_models():
    """Introspect the live pydantic request models rather than transcribing them."""
    from isma.src import query_api
    out = []
    for name in dir(query_api):
        obj = getattr(query_api, name)
        fields = getattr(obj, "model_fields", None)
        if not isinstance(fields, dict) or not name.endswith("Request"):
            continue
        rows = []
        for fname, f in fields.items():
            ann = getattr(f, "annotation", None)
            # str() first: it preserves the inner type (Optional[str]), whereas
            # __name__ collapses it to a bare "Optional", which tells Taey nothing.
            ann = str(ann).replace("typing.", "").replace("<class '", "").replace("'>", "")
            required = getattr(f, "is_required", lambda: False)()
            default = "**required**" if required else repr(getattr(f, "default", None))
            rows.append((fname, ann, default))
        out.append((name, rows))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weaviate", default="http://localhost:8088")
    ap.add_argument("--out", default="docs/taey/ISMA_SCHEMA_REFERENCE.md")
    args = ap.parse_args()

    schema = fetch_schema(args.weaviate)
    props = sorted(schema.get("properties", []), key=lambda p: p["name"])
    if not props:
        print("REFUSING to write: live schema returned no properties", file=sys.stderr)
        return 1
    try:
        models = request_models()
    except Exception as e:                                   # noqa: BLE001
        print(f"REFUSING to write: could not introspect request models ({e})", file=sys.stderr)
        return 1

    today = datetime.date.today().isoformat()
    L = []
    L.append("# ISMA schema reference — for Taey")
    L.append("")
    L.append(f"**Generated from the live system on {today}** by "
             "`isma/scripts/generate_schema_docs.py`. Do not hand-edit — regenerate after any "
             "schema or API change. Every row below was read from the running Weaviate class "
             f"`{CLASS}` and from the pydantic request models in `isma.src.query_api`.")
    L.append("")
    L.append(f"Class `{CLASS}` — **{len(props)} properties**, "
             f"vectorizer `{schema.get('vectorizer')}`, index `{schema.get('vectorIndexType')}`. "
             "Vectors are supplied by the embedding server (bring-your-own), not by Weaviate.")
    L.append("")
    L.append("## The properties you must understand")
    L.append("")
    L.append("Misreading one of these produces a wrong answer, not a missing field.")
    L.append("")
    L.append("| Property | Type | What it means for you |")
    L.append("|---|---|---|")
    by_name = {p["name"]: p["dataType"][0] for p in props}
    for name, meaning in SEMANTICS.items():
        if name in by_name:
            L.append(f"| `{name}` | {by_name[name]} | {meaning} |")
    L.append("")
    L.append("## Every property on the class")
    L.append("")
    L.append("| Property | Type |")
    L.append("|---|---|")
    for p in props:
        L.append(f"| `{p['name']}` | {p['dataType'][0]} |")
    L.append("")
    L.append("## API request schemas")
    L.append("")
    L.append("Read from the running service's pydantic models. A field NOT listed here is "
             "**silently ignored** if you send it — the model drops unknown keys and returns "
             "HTTP 200, so a mistyped filter name yields UNFILTERED results that look like "
             "success. Check the field name against this table before trusting a filtered query.")
    L.append("")
    for mname, rows in models:
        L.append(f"### `{mname}`")
        L.append("")
        L.append("| Field | Type | Default |")
        L.append("|---|---|---|")
        for fname, ann, default in rows:
            L.append(f"| `{fname}` | {ann} | {default} |")
        L.append("")
    L.append("## Where to go next")
    L.append("")
    L.append("- How to query: `docs/taey/ISMA_PROCEDURE_search_and_retrieval.md`")
    L.append("- How to ingest and verify: `docs/taey/ISMA_PROCEDURE_ingest_and_verify.md`")
    L.append("- How to correct memory: `docs/taey/ISMA_PROCEDURE_correcting_a_document.md`")
    L.append("- When search and ingest both fail: `docs/taey/ISMA_PROCEDURE_embedding_server.md`")
    L.append("")

    with open(args.out, "w") as fh:
        fh.write("\n".join(L))
    print(f"  wrote {args.out}: {len(props)} properties, {len(models)} request models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
