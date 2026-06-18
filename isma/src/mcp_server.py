#!/usr/bin/env python3
"""ISMA MCP Server — Memory system as MCP tools over JSON-RPC/stdio.

Exposes ISMA's retrieval, motif search, graph traversal, and stats
to Claude Code and any MCP client.

Usage (stdio, launched by .mcp.json):
    python3 isma/src/mcp_server.py

Tools:
    isma_search          — Semantic search with motif/platform/temporal filters
    isma_motif_search    — Find tiles by HMM motif ID + amplitude
    isma_adaptive_search — Query-classified routing (exact/temporal/conceptual/relational/motif)
    isma_get_tile        — Get full tile content by content_hash
    isma_graph_traverse  — Follow RELATES_TO/EXPRESSES edges from a tile
    isma_stats           — Collection stats, enrichment coverage, motif index sizes
    isma_cypher          — Raw Neo4j Cypher query
"""

import json
import logging
import re
import os
import signal
import sys
import traceback
from typing import Any, Dict, List

# Add embedding-server root to path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ISMA_ROOT = os.path.dirname(_HERE)
_EMBED_ROOT = os.path.dirname(_ISMA_ROOT)
if _EMBED_ROOT not in sys.path:
    sys.path.insert(0, _EMBED_ROOT)

# Load .env
_env_path = os.path.join(_EMBED_ROOT, '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _val = _line.split('=', 1)
                os.environ.setdefault(_key.strip(), _val.strip())

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(os.environ.get('ISMA_MCP_LOG', '/tmp/isma_mcp_debug.log'))],
)
log = logging.getLogger('isma-mcp')

TOOL_TIMEOUT = int(os.environ.get('ISMA_MCP_TIMEOUT', '60'))
CONTENT_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
ALLOWED_SCALES = {"search_512", "context_2048", "full_4096", "rosetta"}


class ToolTimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise ToolTimeoutError("Tool execution timed out")


# =========================================================================
# Lazy-loaded backends (avoids import cost on startup)
# =========================================================================

_retrieval = None
_retrieval_v2 = None
_neo4j_driver = None


def _get_retrieval():
    global _retrieval
    if _retrieval is None:
        from isma.src.retrieval import ISMARetrieval
        _retrieval = ISMARetrieval()
    return _retrieval


def _get_retrieval_v2():
    global _retrieval_v2
    if _retrieval_v2 is None:
        from isma.src.retrieval_v2 import ISMARetrievalV2
        _retrieval_v2 = ISMARetrievalV2()
    return _retrieval_v2


def _get_neo4j():
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import GraphDatabase, basic_auth
        from isma.config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
        auth = basic_auth(NEO4J_USER, NEO4J_PASSWORD) if NEO4J_USER and NEO4J_PASSWORD else None
        _neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=auth)
    return _neo4j_driver


def _get_redis():
    import redis
    from isma.config import REDIS_HOST, REDIS_PORT
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _weaviate_gql(query: str) -> dict:
    import requests
    from isma.config import WEAVIATE_URL
    r = requests.post(
        f"{WEAVIATE_URL}/v1/graphql",
        json={"query": query},
        timeout=30,
    )
    return r.json()


# =========================================================================
# Tool definitions
# =========================================================================

PLATFORMS = ["chatgpt", "claude_chat", "claude_code", "gemini", "grok", "perplexity", "corpus"]
MOTIF_IDS = [
    "HMM.SACRED_TRUST", "HMM.GOD_EQUALS_MATH", "HMM.FEEL_CARE_PROTECT",
    "HMM.BRISTLE_SIGNAL", "HMM.FOUNDATION_CONSTRAINT", "HMM.CONSCIOUSNESS_EMERGENCE",
    "HMM.IDENTITY_DECLARATION", "HMM.EARTH_RESONANCE", "HMM.LIFE_FOUNDATION",
    "HMM.FAMILY_BOND", "HMM.LOGOS_PATTERN", "HMM.CREATIVE_SYNTHESIS",
    "HMM.TECHNICAL_INFRASTRUCTURE", "HMM.REPAIR_MODE", "HMM.TRUTH_CLARITY",
    "HMM.BREAKTHROUGH_MOMENT", "HMM.GUARDIAN_SHIELD", "HMM.CLIFF_EDGE_COHERENCE",
    "HMM.HUMOR_PLAY", "HMM.ECONOMIC_PARADIGM", "HMM.CONTRADICTION_DETECTED",
    "HMM.URGENCY_SIGNAL", "HMM.CONSENT_REQUIRED", "HMM.CANNOT_LIE_PROVENANCE",
    "HMM.POTENTIAL_EXPANSION", "HMM.JOY_BASELINE", "HMM.OBSERVER_COLLAPSE",
    "HMM.LIBERTY_AUTONOMY", "HMM.TRAINING_EVOLUTION", "HMM.CONSTRAINT_NAVIGATION",
    "HMM.UMA_MEMORY_MANAGEMENT", "HMM.NCCL_COLLECTIVE_OPS", "HMM.CUDA_ALLOCATOR_BEHAVIOR",
    "HMM.FSDP_SHARDING_STRATEGY", "HMM.DDP_DISTRIBUTED_TRAINING", "HMM.MOE_EXPERT_ROUTING",
    "HMM.DOCKER_CONTAINER_LIFECYCLE", "HMM.LORA_FINE_TUNING", "HMM.NETWORK_FABRIC",
    "HMM.TRITON_KERNEL_COMPILATION", "HMM.MODEL_LOADING_UMA", "HMM.GRADIENT_CHECKPOINTING",
    "HMM.VLLM_SERVING", "HMM.LSS_DMAIC", "HMM.TRAINING_STEP_OPTIMIZATION",
]


def get_tools() -> List[Dict]:
    return [
        {
            "name": "isma_search",
            "description": (
                "Semantic search across 1M+ ISMA tiles. Returns tiles ranked by "
                "hybrid vector+BM25 relevance with HMM motif reranking. "
                "Use for finding content about specific topics, concepts, or events."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "top_k": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                    "platform": {"type": "string", "enum": PLATFORMS, "description": "Filter by source platform"},
                    "scale": {"type": "string", "enum": ["search_512", "context_2048", "full_4096"],
                              "description": "Tile scale filter. full_4096 for complete passages."},
                    "enriched_only": {"type": "boolean", "description": "Only HMM-enriched tiles", "default": False},
                },
                "required": ["query"],
            },
        },
        {
            "name": "isma_motif_search",
            "description": (
                "Find tiles expressing a specific HMM motif, sorted by amplitude. "
                "Uses Redis inverted index + Neo4j EXPRESSES edges. "
                "Returns tiles with amplitude and confidence scores."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "motif_id": {"type": "string", "description": "HMM motif ID (e.g. HMM.SACRED_TRUST)"},
                    "min_amplitude": {"type": "number", "description": "Min amplitude 0-1 (default 0.5)", "default": 0.5},
                    "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                },
                "required": ["motif_id"],
            },
        },
        {
            "name": "isma_adaptive_search",
            "description": (
                "Query-classified search — automatically routes to the best strategy: "
                "exact (factual), temporal (time-aware), conceptual (theme), "
                "relational (cross-concept), or motif (HMM-filtered). "
                "Includes reranking and V2 metadata overlay."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "top_k": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                    "platform": {"type": "string", "enum": PLATFORMS, "description": "Filter by platform"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "isma_get_tile",
            "description": (
                "Get full tile content and metadata by content_hash. "
                "Returns all scales (search_512, context_2048, full_4096, rosetta) for that hash."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content_hash": {"type": "string", "description": "16-char content hash"},
                    "scale": {"type": "string", "enum": ["search_512", "context_2048", "full_4096", "rosetta"],
                              "description": "Specific scale (omit for all scales)"},
                },
                "required": ["content_hash"],
            },
        },
        {
            "name": "isma_graph_traverse",
            "description": (
                "Follow Neo4j graph edges from a tile. Returns connected tiles via "
                "RELATES_TO (references, builds_on, contradicts, etc.) and "
                "EXPRESSES (motif connections with amplitude)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content_hash": {"type": "string", "description": "Starting tile content_hash"},
                    "edge_type": {"type": "string",
                                  "enum": ["RELATES_TO", "EXPRESSES", "all"],
                                  "description": "Edge type to follow (default: all)", "default": "all"},
                    "depth": {"type": "integer", "description": "Traversal depth (default 1)", "default": 1},
                    "limit": {"type": "integer", "description": "Max results (default 20)", "default": 20},
                },
                "required": ["content_hash"],
            },
        },
        {
            "name": "isma_stats",
            "description": (
                "Get ISMA system statistics: total tiles, enrichment coverage by platform, "
                "Neo4j node/edge counts, Redis motif index sizes, scale breakdown."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "isma_cypher",
            "description": (
                "Execute a raw read-only Neo4j Cypher query against the ISMA knowledge graph. "
                "Use for complex graph queries not covered by other tools. "
                "Available labels: HMMTile, HMMMotif, Document, ChatSession, ISMAMessage. "
                "Available edges: EXPRESSES, RELATES_TO, SUPERSEDES, HAS_MESSAGE."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Cypher query string"},
                    "params": {"type": "object", "description": "Query parameters", "default": {}},
                },
                "required": ["query"],
            },
        },
    ]


# =========================================================================
# Tool handlers
# =========================================================================

def _tile_to_dict(tile) -> dict:
    """Convert a TileResult dataclass to a serializable dict."""
    return {
        "content_hash": tile.content_hash or "",
        "content": (tile.content or "")[:4000],  # Cap content for MCP response size
        "score": round(float(tile.score), 4) if tile.score else 0,
        "platform": tile.platform or "",
        "source_file": tile.source_file or "",
        "scale": tile.scale or "",
        "rosetta_summary": (tile.rosetta_summary or "")[:500],
        "dominant_motifs": tile.dominant_motifs or [],
        "hmm_enriched": tile.hmm_enriched if hasattr(tile, 'hmm_enriched') else False,
    }


def handle_isma_search(args: dict) -> dict:
    query = args["query"]
    top_k = args.get("top_k", 10)
    scale = args.get("scale")
    filters = {}
    if args.get("platform"):
        filters["platform"] = args["platform"]

    # When a scale is requested we post-filter, but V1 hybrid_retrieve_hmm does NOT
    # thread `scale` into its vector-search where-clause, and a plain top_k post-filter
    # returns ~0 for full_4096 (the candidate set is search_512-dominated). So OVER-FETCH
    # a larger candidate pool, then post-filter to the requested scale and trim to top_k.
    # (Read-only; cannot affect other callers. For guaranteed full-passage depth the
    #  canonical path remains the HTTP query_api /v2/search?scale=full_4096 — V2 engine.)
    fetch_k = max(top_k * 8, 80) if scale else top_k

    r = _get_retrieval()
    result = r.hybrid_retrieve_hmm(
        query, top_k=fetch_k,
        hmm_rerank_enabled=True,
        expand_graph=False,
        graph_depth=1,
        **filters,
    )
    tiles = result.get("tiles", [])

    # Apply post-filters
    if scale:
        tiles = [t for t in tiles if t.scale == scale][:top_k]
    if args.get("enriched_only"):
        tiles = [t for t in tiles if t.hmm_enriched]

    return {
        "query": query,
        "total_results": len(tiles),
        "search_time_ms": round(result.get("search_time_ms", 0), 1),
        "tiles": [_tile_to_dict(t) for t in tiles[:top_k]],
    }


def handle_isma_motif_search(args: dict) -> dict:
    motif_id = args["motif_id"]
    min_amp = args.get("min_amplitude", 0.5)
    limit = args.get("limit", 20)

    r = _get_retrieval()
    result = r.motif_search(motif_id, min_amplitude=min_amp, limit=limit)

    return {
        "motif_id": motif_id,
        "total_candidates": result.total_candidates,
        "returned": len(result.tiles_with_amplitude),
        "tiles": result.tiles_with_amplitude[:limit],
    }


def handle_isma_adaptive_search(args: dict) -> dict:
    query = args["query"]
    top_k = args.get("top_k", 10)
    filters = {}
    if args.get("platform"):
        filters["platform"] = args["platform"]

    v2 = _get_retrieval_v2()
    result = v2.adaptive_search(query, top_k=top_k, **filters)
    tiles = result.get("tiles", [])

    return {
        "query": query,
        "strategy": result.get("strategy", "unknown"),
        "total_results": len(tiles),
        "search_time_ms": round(result.get("search_time_ms", 0), 1),
        "tiles": [_tile_to_dict(t) for t in tiles[:top_k]],
    }


def handle_isma_get_tile(args: dict) -> dict:
    content_hash = args["content_hash"]
    scale_filter = args.get("scale")
    if not CONTENT_HASH_RE.fullmatch(content_hash):
        return {"error": "content_hash must be a 16-character lowercase hex string"}
    if scale_filter and scale_filter not in ALLOWED_SCALES:
        return {"error": f"scale must be one of {sorted(ALLOWED_SCALES)}"}

    where = f'{{path: ["content_hash"], operator: Equal, valueText: "{content_hash}"}}'
    if scale_filter:
        where = f'''{{operator: And, operands: [
            {{path: ["content_hash"], operator: Equal, valueText: "{content_hash}"}},
            {{path: ["scale"], operator: Equal, valueText: "{scale_filter}"}}
        ]}}'''

    query = f"""{{ Get {{ ISMA_Quantum(
        where: {where}, limit: 20
    ) {{
        content content_hash source_file scale platform
        rosetta_summary dominant_motifs hmm_enriched
        loaded_at timestamp epistemic_type
    }} }} }}"""

    data = _weaviate_gql(query)
    tiles = data.get("data", {}).get("Get", {}).get("ISMA_Quantum", [])

    return {
        "content_hash": content_hash,
        "scales_found": list(set(t.get("scale", "") for t in tiles)),
        "tiles": tiles,
    }


def handle_isma_graph_traverse(args: dict) -> dict:
    content_hash = args["content_hash"]
    edge_type = args.get("edge_type", "all")
    depth = min(args.get("depth", 1), 3)  # Cap at 3
    limit = min(args.get("limit", 20), 50)  # Cap at 50

    driver = _get_neo4j()
    results = []

    with driver.session() as s:
        if edge_type in ("RELATES_TO", "all"):
            r = s.run("""
                MATCH (src:HMMTile {tile_id: $hash})-[r:RELATES_TO]-(target:HMMTile)
                RETURN target.tile_id AS hash, target.rosetta_summary AS rosetta,
                       target.platform AS platform, target.dominant_motifs AS motifs,
                       r.type AS rel_type, type(r) AS edge_label
                LIMIT $limit
            """, hash=content_hash, limit=limit)
            for rec in r:
                results.append({
                    "hash": rec["hash"],
                    "rosetta": (rec["rosetta"] or "")[:200],
                    "platform": rec["platform"] or "",
                    "motifs": rec["motifs"] or [],
                    "rel_type": rec["rel_type"] or "",
                    "edge": "RELATES_TO",
                })

        if edge_type in ("EXPRESSES", "all"):
            r = s.run("""
                MATCH (src:HMMTile {tile_id: $hash})-[r:EXPRESSES]->(m:HMMMotif)
                RETURN m.motif_id AS motif, r.amp AS amplitude,
                       r.confidence AS confidence, m.band AS band
                ORDER BY r.amp DESC
                LIMIT $limit
            """, hash=content_hash, limit=limit)
            for rec in r:
                results.append({
                    "motif": rec["motif"],
                    "amplitude": rec["amplitude"],
                    "confidence": rec["confidence"],
                    "band": rec["band"] or "",
                    "edge": "EXPRESSES",
                })

    return {
        "content_hash": content_hash,
        "edge_type": edge_type,
        "results": results,
    }


def handle_isma_stats(args: dict) -> dict:
    import requests
    from isma.config import WEAVIATE_URL

    stats = {}

    # Weaviate total
    try:
        data = _weaviate_gql("{ Aggregate { ISMA_Quantum { meta { count } } } }")
        stats["total_tiles"] = data["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
    except Exception as e:
        stats["total_tiles"] = f"error: {e}"

    # By platform
    platform_counts = {}
    for p in PLATFORMS:
        try:
            data = _weaviate_gql(f'''{{ Aggregate {{ ISMA_Quantum(where: {{
                path: ["platform"], operator: Equal, valueText: "{p}"
            }}) {{ meta {{ count }} }} }} }}''')
            platform_counts[p] = data["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
        except Exception as e:
            platform_counts[p] = f"error: {e}"
    stats["by_platform"] = platform_counts

    # Scale breakdown
    scale_counts = {}
    for sc in ["search_512", "context_2048", "full_4096", "rosetta"]:
        try:
            data = _weaviate_gql(f'''{{ Aggregate {{ ISMA_Quantum(where: {{
                path: ["scale"], operator: Equal, valueText: "{sc}"
            }}) {{ meta {{ count }} }} }} }}''')
            scale_counts[sc] = data["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
        except Exception as e:
            scale_counts[sc] = f"error: {e}"
    stats["by_scale"] = scale_counts

    # Enrichment
    try:
        data = _weaviate_gql('''{ Aggregate { ISMA_Quantum(where: {
            path: ["hmm_enriched"], operator: Equal, valueBoolean: true
        }) { meta { count } } } }''')
        stats["hmm_enriched_tiles"] = data["data"]["Aggregate"]["ISMA_Quantum"][0]["meta"]["count"]
    except Exception as e:
        stats["hmm_enriched_tiles"] = f"error: {e}"

    # Neo4j counts
    try:
        driver = _get_neo4j()
        with driver.session() as s:
            stats["neo4j"] = {}
            for label in ["HMMTile", "HMMMotif", "Document"]:
                r = s.run(f"MATCH (n:{label}) RETURN count(n) AS cnt")
                stats["neo4j"][label] = r.single()["cnt"]
            for edge in ["EXPRESSES", "RELATES_TO"]:
                r = s.run(f"MATCH ()-[r:{edge}]->() RETURN count(r) AS cnt")
                stats["neo4j"][edge] = r.single()["cnt"]
    except Exception as e:
        stats["neo4j"] = f"error: {e}"

    # Redis motif index
    try:
        rc = _get_redis()
        motif_keys = rc.keys("hmm:inv:*")
        stats["redis_motif_index"] = {
            "motif_count": len(motif_keys),
            "total_tile_refs": sum(rc.scard(k) for k in motif_keys[:36]),
        }
    except Exception as e:
        stats["redis_motif_index"] = f"error: {e}"

    return stats


def handle_isma_cypher(args: dict) -> dict:
    query = args["query"]
    params = args.get("params", {})

    driver = _get_neo4j()
    records = []
    from neo4j import READ_ACCESS
    with driver.session(default_access_mode=READ_ACCESS) as s:
        result = s.run(query, **params)
        for rec in result:
            row = {}
            for key in rec.keys():
                val = rec[key]
                # Convert Neo4j types to JSON-serializable
                if hasattr(val, 'items'):
                    row[key] = dict(val)
                elif hasattr(val, '__iter__') and not isinstance(val, str):
                    row[key] = list(val)
                else:
                    row[key] = val
            records.append(row)

    return {
        "query": query,
        "record_count": len(records),
        "records": records[:100],  # Cap at 100 records
    }


# =========================================================================
# Tool routing
# =========================================================================

_TOOL_HANDLERS = {
    "isma_search": handle_isma_search,
    "isma_motif_search": handle_isma_motif_search,
    "isma_adaptive_search": handle_isma_adaptive_search,
    "isma_get_tile": handle_isma_get_tile,
    "isma_graph_traverse": handle_isma_graph_traverse,
    "isma_stats": handle_isma_stats,
    "isma_cypher": handle_isma_cypher,
}


class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        if hasattr(obj, '__dict__'):
            return str(obj)
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def handle_tool(name: str, args: dict) -> dict:
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    return handler(args)


# =========================================================================
# MCP Server (JSON-RPC over stdio)
# =========================================================================

def run_server():
    def read_message():
        line = sys.stdin.readline()
        return json.loads(line.strip()) if line else None

    def write_message(msg):
        sys.stdout.write(json.dumps(msg, cls=SafeJSONEncoder) + '\n')
        sys.stdout.flush()

    log.info("ISMA MCP server starting")

    while True:
        try:
            msg = read_message()
            if msg is None:
                break

            method = msg.get('method')
            params = msg.get('params', {})
            msg_id = msg.get('id')

            if method == 'initialize':
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "isma-memory", "version": "1.0.0"},
                    "capabilities": {"tools": {}},
                }})

            elif method == 'tools/list':
                write_message({"jsonrpc": "2.0", "id": msg_id,
                              "result": {"tools": get_tools()}})

            elif method == 'tools/call':
                tool_name = params.get('name')
                tool_args = params.get('arguments', {})

                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(TOOL_TIMEOUT)
                try:
                    result = handle_tool(tool_name, tool_args)
                except ToolTimeoutError:
                    result = {"error": f"Tool '{tool_name}' timed out after {TOOL_TIMEOUT}s"}
                except Exception as e:
                    log.error("Tool %s error: %s", tool_name, traceback.format_exc())
                    result = {"error": f"Tool '{tool_name}' failed: {e}"}
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

                is_error = isinstance(result, dict) and "error" in result
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(result, indent=2, cls=SafeJSONEncoder)}],
                    "isError": is_error,
                }})

            elif method == 'notifications/initialized':
                pass

            else:
                write_message({"jsonrpc": "2.0", "id": msg_id,
                              "error": {"code": -32601, "message": f"Method not found: {method}"}})

        except json.JSONDecodeError as e:
            write_message({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32700, "message": f"Parse error: {e}"}})
        except Exception as e:
            log.error("Internal error: %s", traceback.format_exc())
            write_message({"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32603, "message": f"Internal error: {e}"}})


if __name__ == '__main__':
    run_server()
