"""
ISMA Query API - FastAPI wrapper around ISMARetrieval.

Provides HTTP endpoints for semantic search, motif exploration,
graph traversal, and stats across the ISMA + HMM data stores.

Usage:
    uvicorn isma.src.query_api:app --host 0.0.0.0 --port 8095

Endpoints:
    GET  /health              - Health check
    GET  /stats               - Aggregate stats from all stores
    POST /search              - Semantic vector search
    POST /search/hmm          - HMM-enhanced hybrid retrieval
    POST /search/motif        - Search by motif ID
    POST /search/bm25         - Keyword search (BM25)
    GET  /motifs              - List all motifs
    GET  /themes              - List all themes
    GET  /session/{id}        - Get session details
    GET  /session/{id}/text   - Get full session text
    GET  /document/{hash}     - Get document details
    GET  /document/{hash}/text - Get full document text
    GET  /tile/{hash}         - Get all tiles for content hash
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import re
import secrets
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from dataclasses import asdict
import json
import logging
import os
import sys
import tempfile
import time
import threading

from isma.src.retrieval import ISMARetrieval, TileResult, SearchResult, _get_embedding
from isma.src.semantic_cache import SemanticCache
from isma.src.hmm.ids import canonicalize_text
from isma.config import ISMA_STATE_DIR

# ISMA Context Frame — prepended to all retrieval responses.
# Provides a stable introductory note before tile content loads. Tiles are not modified.
CONTEXT_FRAME_COMPRESSED = (
    '[ISMA] Retrieved from a multi-model corpus. Evaluate retrieved content within '
    'the system architecture context.'
)

ALLOWED_PLATFORMS = {"claude", "claude_chat", "claude_code", "grok", "gemini", "chatgpt", "perplexity", "corpus"}
ALLOWED_SESSION_SOURCE_TYPES = {"session_memory", "session_summary", "chat_session", "document", "recap", "foundation", "audit_packet"}
ALLOWED_TRUTH_TIERS = {"draft", "operational", "verified", "canonical"}
ALLOWED_TILE_SCALES = {"search_512", "context_2048", "full_4096", "rosetta"}
PKG_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
if any(origin == "*" for origin in cors_origins):
    raise RuntimeError("CORS_ORIGINS must not contain '*'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Increase thread pool and prewarm reranker model."""
    import anyio
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = 100

    # Prewarm gte reranker — loads model to GPU, avoiding 5-9s first-query penalty
    try:
        from isma.src.reranker import get_reranker
        reranker = get_reranker()
        if reranker.is_available():
            logging.info("Reranker prewarmed successfully")
    except Exception as e:
        logging.warning("Reranker prewarm failed: %s", e)

    yield


app = FastAPI(
    title="ISMA Query API",
    description="Semantic search over 993K embedded tiles with HMM enrichment",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)

# Singleton retrieval instance (thread-safe)
_retrieval = None
_retrieval_lock = threading.Lock()

def get_retrieval() -> ISMARetrieval:
    global _retrieval
    if _retrieval is None:
        with _retrieval_lock:
            if _retrieval is None:
                _retrieval = ISMARetrieval()
    return _retrieval


# Singleton cache instance (thread-safe, avoids new Redis connection per request)
_cache = None
_cache_lock = threading.Lock()

def _get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = SemanticCache()
    return _cache


# ── Request Models ──────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    expand_parents: bool = False
    include_superseded: Optional[bool] = False
    platform: Optional[str] = None
    source_type: Optional[str] = None
    scale: Optional[str] = None
    session_id: Optional[str] = None
    document_id: Optional[str] = None
    has_artifacts: Optional[bool] = None
    has_thinking: Optional[bool] = None
    layer: Optional[int] = None
    min_priority: Optional[float] = None
    model: Optional[str] = None
    dominant_motifs: Optional[List[str]] = None
    hmm_enriched: Optional[bool] = None
    min_hmm_phi: Optional[float] = None
    min_hmm_trust: Optional[float] = None
    theme_id: Optional[str] = None
    motif_band: Optional[str] = None


class HMMSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    hmm_rerank: bool = True
    expand_graph: bool = False
    graph_depth: int = 1
    expand_to_session: bool = False
    expand_to_document: bool = False
    rosetta_weight: float = 0.3
    motif_weight: float = 0.2
    query_type: str = "default"
    instruction: Optional[str] = None
    include_superseded: Optional[bool] = False
    platform: Optional[str] = None
    source_type: Optional[str] = None
    hmm_enriched: Optional[bool] = None


class MotifSearchRequest(BaseModel):
    motif_id: str
    min_amplitude: float = 0.5
    top_k: int = Field(default=20, ge=1, le=100)
    platform: Optional[str] = None


class BM25Request(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    include_superseded: Optional[bool] = False
    platform: Optional[str] = None
    source_type: Optional[str] = None


class HMMStoreRequest(BaseModel):
    platform: str
    content: str = Field(..., max_length=500000)
    pkg_id: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────

def _tile_to_dict(t: TileResult) -> dict:
    d = asdict(t)
    # Trim empty fields for cleaner output
    return {k: v for k, v in d.items()
            if v and v != 0 and v != 0.0 and v != [] and v != ""}


def _search_result_to_dict(sr: SearchResult) -> dict:
    return {
        "context_frame": CONTEXT_FRAME_COMPRESSED,
        "query": sr.query,
        "total_tokens": sr.total_tokens,
        "search_time_ms": round(sr.search_time_ms, 1),
        "count": len(sr.tiles),
        "tiles": [_tile_to_dict(t) for t in sr.tiles],
    }


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected_api_key = os.environ.get("ISMA_API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=503,
            detail="writes disabled: set ISMA_API_KEY to enable",
        )
    if not isinstance(x_api_key, str):
        raise HTTPException(status_code=401, detail="invalid api key")
    if not secrets.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(status_code=401, detail="invalid api key")


# ── Endpoints ───────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "isma-query-api", "timestamp": time.time()}


@app.get("/stats")
def stats():
    r = get_retrieval()
    return r.stats()


@app.post("/search")
def search(req: SearchRequest):
    r = get_retrieval()
    filters = {}
    for field_name in ["platform", "source_type", "scale", "session_id",
                       "document_id", "has_artifacts", "has_thinking",
                       "layer", "min_priority", "model", "dominant_motifs",
                       "hmm_enriched", "min_hmm_phi", "min_hmm_trust",
                       "theme_id", "motif_band"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    result = r.search(
        query=req.query,
        top_k=req.top_k,
        expand_parents=req.expand_parents,
        include_superseded=req.include_superseded,
        **filters,
    )
    return _search_result_to_dict(result)


@app.post("/search/hmm")
def search_hmm(req: HMMSearchRequest):
    r = get_retrieval()
    filters = {}
    for field_name in ["platform", "source_type", "hmm_enriched",
                       "include_superseded"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    result = r.hybrid_retrieve_hmm(
        query=req.query,
        top_k=req.top_k,
        hmm_rerank_enabled=req.hmm_rerank,
        expand_graph=req.expand_graph,
        graph_depth=req.graph_depth,
        expand_to_session=req.expand_to_session,
        expand_to_document=req.expand_to_document,
        rosetta_weight=req.rosetta_weight,
        motif_weight=req.motif_weight,
        query_type=req.query_type,
        instruction=req.instruction or "",
        **filters,
    )

    # Convert tiles in result
    tiles = result.get("tiles", [])
    result["tiles"] = [_tile_to_dict(t) if isinstance(t, TileResult) else t
                       for t in tiles]
    result["count"] = len(result["tiles"])
    result["search_time_ms"] = round(result.get("search_time_ms", 0), 1)

    # Convert session/document results
    for key in ["sessions", "documents", "graph_expansions"]:
        sub = result.get(key, {})
        if sub:
            for k, v in sub.items():
                if hasattr(v, '__dataclass_fields__'):
                    sub[k] = asdict(v)
    result["context_frame"] = CONTEXT_FRAME_COMPRESSED
    return result


@app.post("/search/motif")
def search_motif(req: MotifSearchRequest):
    r = get_retrieval()
    result = r.motif_search(
        motif_id=req.motif_id,
        min_amplitude=req.min_amplitude,
        limit=req.top_k,
    )
    if hasattr(result, '__dataclass_fields__'):
        return asdict(result)
    return result


@app.post("/search/bm25")
def search_bm25(req: BM25Request):
    r = get_retrieval()
    filters = {}
    if req.include_superseded is not None:
        filters["include_superseded"] = req.include_superseded
    if req.platform:
        filters["platform"] = req.platform
    if req.source_type:
        filters["source_type"] = req.source_type

    result = r.search_bm25(
        query=req.query,
        top_k=req.top_k,
        **filters,
    )
    return _search_result_to_dict(result)


@app.get("/motifs")
def list_motifs(band: Optional[str] = None):
    return ISMARetrieval.list_motifs(band=band)


@app.get("/themes")
def list_themes():
    return ISMARetrieval.list_themes()


@app.get("/session/{session_id}")
def get_session(session_id: str):
    r = get_retrieval()
    result = r.get_session(session_id)
    if result is None:
        return {"error": "Session not found"}
    return asdict(result)


@app.get("/session/{session_id}/text")
def get_session_text(session_id: str):
    r = get_retrieval()
    text = r.get_session_full_text(session_id)
    return {"session_id": session_id, "text": text, "length": len(text)}


@app.get("/session/{session_id}/exchanges")
def get_exchanges(session_id: str):
    r = get_retrieval()
    exchanges = r.get_exchanges(session_id)
    return {"session_id": session_id, "count": len(exchanges),
            "exchanges": [asdict(e) for e in exchanges]}


@app.get("/document/search/{name}")
def search_documents(name: str, limit: int = Query(default=20, ge=1, le=100)):
    """Search documents by filename pattern (case-insensitive).

    Returns matching documents with metadata including content_hash.
    Use /document/{content_hash}/text to get full text of a specific match.
    """
    r = get_retrieval()
    docs = r.search_documents(name, limit=limit)
    return {
        "query": name,
        "count": len(docs),
        "documents": [asdict(d) for d in docs],
    }


@app.get("/document/retrieve/{name}")
def retrieve_document(name: str):
    """One-shot full document retrieval by name.

    Searches for the document, reconstructs full text from tiles
    with de-overlapped phi-tile assembly. Returns the highest-priority
    match with complete text content.
    """
    r = get_retrieval()
    result = r.get_document_full_text(name)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    result["context_frame"] = CONTEXT_FRAME_COMPRESSED
    return result


@app.get("/document/{content_hash}")
def get_document(content_hash: str):
    r = get_retrieval()
    result = r.get_document(content_hash)
    if result is None:
        return {"error": "Document not found"}
    return asdict(result)


@app.get("/document/{content_hash}/text")
def get_document_text(content_hash: str):
    r = get_retrieval()
    text = r.get_full_text(content_hash)
    return {"content_hash": content_hash, "text": text, "length": len(text)}


@app.get("/tiles/{content_hash}")
def get_tiles(
    content_hash: str,
    scale: Optional[str] = None,
    include_superseded: bool = False,
):
    r = get_retrieval()
    tiles = r.get_tiles_for_content(
        content_hash,
        scale=scale,
        include_superseded=include_superseded,
    )
    return {"content_hash": content_hash, "count": len(tiles),
            "tiles": [_tile_to_dict(t) for t in tiles]}


@app.get("/sessions")
def list_sessions(
    platform: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    r = get_retrieval()
    sessions = r.list_sessions(platform=platform, limit=limit)
    total = r.count_sessions(platform=platform)
    return {
        "total": total,
        "limit": limit,
        "sessions": [asdict(s) for s in sessions],
    }


@app.get("/documents")
def list_documents(
    layer: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    r = get_retrieval()
    docs = r.list_documents(layer=layer, limit=limit)
    total = r.count_documents(layer=layer)
    return {
        "total": total,
        "limit": limit,
        "documents": [asdict(d) for d in docs],
    }


# ── V2 Endpoints (Shadow Deployment) ─────────────────────────

class V2SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, ge=1, le=100)
    rerank: bool = True
    query_type: str = "default"
    instruction: Optional[str] = None
    include_superseded: Optional[bool] = False
    platform: Optional[str] = None
    source_type: Optional[str] = None
    hmm_enriched: Optional[bool] = None


@app.get("/v2/stats")
def v2_stats():
    from isma.src.retrieval_v2 import get_retrieval_v2
    r = get_retrieval_v2()
    return r.stats()


@app.post("/v2/search")
def v2_search(req: V2SearchRequest):
    from isma.src.retrieval_v2 import get_retrieval_v2
    r = get_retrieval_v2()

    if not r.is_available():
        raise HTTPException(status_code=503, detail="V2 class not available")

    filters = {}
    for field_name in ["platform", "source_type", "hmm_enriched",
                       "include_superseded"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    result = r.search(req.query, top_k=req.top_k, **filters)
    return _search_result_to_dict(result)


@app.post("/v2/search/hmm")
def v2_search_hmm(req: V2SearchRequest):
    """V1-Plus adaptive search. Formerly called hybrid_search() (V1 only).

    Fixed (2026-03-06 Phase 6B close): ChatGPT audit found hybrid_search() bypasses
    adaptive_search() — production path ≠ benchmarked path. Now routes to adaptive_search()
    for consistent V1-Plus behavior (query classification, motif routing, temporal decay).
    """
    from isma.src.retrieval_v2 import get_retrieval_v2
    r = get_retrieval_v2()

    if not r.is_available():
        raise HTTPException(status_code=503, detail="V2 class not available")

    filters = {}
    for field_name in ["platform", "source_type", "hmm_enriched",
                       "include_superseded"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    result = r.adaptive_search(
        req.query,
        top_k=req.top_k,
        **filters,
    )

    tiles = result.get("tiles", [])
    result["tiles"] = [_tile_to_dict(t) if isinstance(t, TileResult) else t for t in tiles]
    result["count"] = len(result["tiles"])
    result["search_time_ms"] = round(result.get("search_time_ms", 0), 1)
    result["context_frame"] = CONTEXT_FRAME_COMPRESSED
    return result


@app.get("/v2/expand/{content_hash}")
def v2_expand(content_hash: str, scale: Optional[str] = "search_512"):
    from isma.src.retrieval_v2 import get_retrieval_v2
    r = get_retrieval_v2()
    tiles = r.expand_passages(content_hash, scale=scale or "search_512")
    return {
        "content_hash": content_hash,
        "scale": scale,
        "count": len(tiles),
        "tiles": [_tile_to_dict(t) for t in tiles],
    }


@app.post("/v2/search/adaptive")
def v2_search_adaptive(req: V2SearchRequest):
    from isma.src.retrieval_v2 import get_retrieval_v2
    from isma.src.semantic_cache import SemanticCache

    r = get_retrieval_v2()
    if not r.is_available():
        raise HTTPException(status_code=503, detail="V2 class not available")

    filters = {}
    for field_name in ["platform", "source_type", "hmm_enriched",
                       "include_superseded"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    # Fetch embedding once — used for both semantic cache lookup and cache store.
    # Without this, cache.get/put bypass _find_similar() and only do exact string match.
    query_embedding = None
    try:
        query_embedding = _get_embedding(req.query)
    except Exception:
        pass  # Embedding failure is non-fatal; cache falls back to exact-match only

    # Phase 5: Check semantic cache first (includes filters in key)
    try:
        cache = _get_cache()
        cached = cache.get(req.query, query_type="adaptive", embedding=query_embedding, top_k=req.top_k, **filters)
        if cached:
            cached_result = cached.get("result", cached)
            cached_result["cache_hit"] = True
            return cached_result
    except Exception:
        pass  # Cache failure is non-fatal

    result = r.adaptive_search(
        req.query,
        top_k=req.top_k,
        **filters,
    )

    tiles = result.get("tiles", [])
    result["tiles"] = [_tile_to_dict(t) if isinstance(t, TileResult) else t for t in tiles]
    result["count"] = len(result["tiles"])
    result["search_time_ms"] = round(result.get("search_time_ms", 0), 1)
    result["cache_hit"] = False

    # Phase 5: Store in cache — use same query_type as read for key consistency
    try:
        cache.put(req.query, result, query_type="adaptive", embedding=query_embedding, top_k=req.top_k, **filters)
    except Exception:
        pass  # Cache failure is non-fatal

    result["context_frame"] = CONTEXT_FRAME_COMPRESSED
    return result


@app.post("/v2/search/retry")
def v2_search_retry(req: V2SearchRequest):
    """Adaptive search with agentic retry on low-quality results.

    If the first attempt returns results with top score < 0.3,
    retries once with expanded strategy and loosened filters.
    """
    from isma.src.retrieval_v2 import get_retrieval_v2
    from isma.src.agentic_retry import retrieval_with_retry

    r = get_retrieval_v2()
    if not r.is_available():
        raise HTTPException(status_code=503, detail="V2 class not available")

    filters = {}
    for field_name in ["platform", "source_type", "hmm_enriched",
                       "include_superseded"]:
        val = getattr(req, field_name)
        if val is not None:
            filters[field_name] = val

    result = retrieval_with_retry(
        req.query,
        top_k=req.top_k,
        **filters,
    )

    tiles = result.get("tiles", [])
    result["tiles"] = [_tile_to_dict(t) if isinstance(t, TileResult) else t for t in tiles]
    result["count"] = len(result["tiles"])
    result["search_time_ms"] = round(result.get("search_time_ms", 0), 1)
    result["context_frame"] = CONTEXT_FRAME_COMPRESSED
    return result


# ── V2 Phase 4: Temporal Truth Endpoints ─────────────────────

@app.get("/v2/timeline/{content_hash}")
def v2_timeline(content_hash: str):
    """Get the temporal version chain for a content_hash.

    Shows all enrichment versions from newest to oldest,
    following SUPERSEDES edges.
    """
    from isma.src.hmm.neo4j_store import HMMNeo4jStore
    store = HMMNeo4jStore()
    try:
        chain = store.get_temporal_chain(content_hash)
        return {
            "content_hash": content_hash,
            "versions": len(chain),
            "chain": chain,
        }
    finally:
        store.close()


@app.get("/v2/contradictions")
def v2_contradictions(
    min_confidence: float = Query(default=0.5, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List detected contradictions across the knowledge base."""
    from isma.src.hmm.neo4j_store import HMMNeo4jStore
    store = HMMNeo4jStore()
    try:
        contradictions = store.get_contradictions(
            min_confidence=min_confidence, limit=limit,
        )
        return {
            "count": len(contradictions),
            "min_confidence": min_confidence,
            "contradictions": contradictions,
        }
    finally:
        store.close()


@app.get("/v2/session/{session_id}/reconstruct")
def v2_reconstruct_session(session_id: str):
    """Reconstruct a session from its HMM-enriched tiles.

    Returns tiles in exchange order, preferring latest versions
    (following SUPERSEDES chains). Includes contradiction info.
    """
    from isma.src.hmm.neo4j_store import HMMNeo4jStore
    store = HMMNeo4jStore()
    try:
        tiles = store.reconstruct_session(session_id)
        return {
            "session_id": session_id,
            "tile_count": len(tiles),
            "tiles": tiles,
        }
    finally:
        store.close()


@app.get("/v2/tile/{tile_id}/contradictions")
def v2_tile_contradictions(tile_id: str):
    """Get all contradictions for a specific tile."""
    from isma.src.hmm.neo4j_store import HMMNeo4jStore
    store = HMMNeo4jStore()
    try:
        contradictions = store.get_tile_contradictions(tile_id)
        return {
            "tile_id": tile_id,
            "count": len(contradictions),
            "contradictions": contradictions,
        }
    finally:
        store.close()


@app.post("/v2/contradictions/check")
def v2_check_contradictions(
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_api_key),
):
    """Trigger batch contradiction verification.

    Scans RELATES_TO {type: 'contradicts'} edges that don't yet
    have a corresponding CONTRADICTS edge and verifies them via
    the cross-encoder reranker.
    """
    from isma.src.contradiction_detector import check_contradictions_batch
    results = check_contradictions_batch(limit=limit)
    return {
        "checked": limit,
        "confirmed": len(results),
        "contradictions": results,
    }


@app.get("/v2/cache/stats")
def v2_cache_stats():
    """Get semantic cache statistics."""
    from isma.src.semantic_cache import SemanticCache
    cache = SemanticCache()
    return cache.stats()


@app.post("/v2/cache/clear")
def v2_cache_clear(_: None = Depends(require_api_key)):
    """Clear all semantic cache entries."""
    from isma.src.semantic_cache import SemanticCache
    cache = SemanticCache()
    cache.clear()
    return {"status": "cleared"}


@app.post("/v2/backfill/session-links")
def v2_backfill_session_links(
    limit: int = Query(default=5000, ge=1, le=50000),
    _: None = Depends(require_api_key),
):
    """Backfill IN_SESSION edges between HMMTiles and ISMASessions.

    Links tiles to their originating sessions via shared content_hash
    in ISMAExchange nodes.
    """
    from isma.src.hmm.neo4j_store import HMMNeo4jStore
    store = HMMNeo4jStore()
    try:
        created = store.backfill_session_links(limit=limit)
        return {
            "created": created,
            "limit": limit,
        }
    finally:
        store.close()


# ── HMM Store Endpoint ────────────────────────────────────────

_hmm_store_imported = False
_process_response = None

def _get_process_response():
    """Lazy import of hmm_store_results.process_response."""
    global _hmm_store_imported, _process_response
    if not _hmm_store_imported:
        scripts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from hmm_store_results import process_response
        _process_response = process_response
        _hmm_store_imported = True
    return _process_response


@app.post("/hmm/store-response")
def hmm_store_response(req: HMMStoreRequest, _: None = Depends(require_api_key)):
    """Store HMM enrichment response in Weaviate + Neo4j + Redis.

    Accepts raw AI response JSON from enrichment runs and calls
    hmm_store_results.process_response() for the triple-write.
    """
    log = logging.getLogger("hmm-store")

    # Validate content is parseable JSON
    try:
        parsed = json.loads(req.content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Content is not valid JSON: {e}")

    # Extract pkg_id from response if not provided
    pkg_id = req.pkg_id or parsed.get("package_id", f"api_{int(time.time())}")

    if req.platform not in ALLOWED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Invalid platform: {req.platform}")

    # Write to temp file (process_response expects a file path)
    response_dir = os.path.join(ISMA_STATE_DIR, "hmm_responses")
    safe_pkg = PKG_ID_RE.sub("_", pkg_id)[:60]

    try:
        os.makedirs(response_dir, exist_ok=True)
        permanent_path = os.path.join(response_dir, f"{safe_pkg}_{req.platform}.json")
        with open(permanent_path, "w") as f:
            json.dump(parsed, f)
        log.info(f"Saved response to {permanent_path}")
    except Exception as e:
        log.warning(f"Failed to save permanent copy: {e}")
        # Fall back to temp file
        fd, permanent_path = tempfile.mkstemp(suffix=".json", dir=response_dir)
        with os.fdopen(fd, "w") as f:
            json.dump(parsed, f)

    # Call the triple-write — 6SIGMA: HTTP 500 on failure, never hide errors
    try:
        process_fn = _get_process_response()
        result = process_fn(
            permanent_path,
            platform=req.platform,
            pkg_id=pkg_id,
        )
        log.info(f"HMM store result: {result}")

        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=f"Storage failed: {result.get('stored',0)}/{result.get('parsed',0)} stored, "
                       f"{result.get('failed',0)} failed. File: {permanent_path}",
            )

        return {
            "success": True,
            "parsed": result.get("parsed", 0),
            "stored": result.get("stored", 0),
            "pkg_id": pkg_id,
            "file": permanent_path,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"HMM store failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"HMM store error: {str(e)}. File: {permanent_path}",
        )


# ── Session Ingestion ─────────────────────────────────────────

class SessionTileRequest(BaseModel):
    """Ingest a session summary as an ISMA tile."""
    content: str = Field(..., min_length=10, max_length=20000, description="Session summary text")
    source_file: str = Field(..., max_length=512, description="Source identifier (e.g. session memory filename)")
    platform: str = Field(default="corpus", description="Platform that produced this session")
    actor: str = Field(default="agent", max_length=128, description="Agent identity")
    session_id: Optional[str] = Field(default=None, description="Session ID for linking")
    source_type: str = Field(default="session_memory", description="Tile source type")
    truth_tier: str = Field(default="operational", description="Truth tier classification")
    scale: str = Field(default="full_4096", description="Tile scale")
    rosetta_summary: Optional[str] = Field(default=None, description="Short summary (auto-generated if omitted)")
    tags: Optional[List[str]] = Field(default=None, description="Optional tags for filtering")


@app.post("/ingest/session")
def ingest_session_tile(req: SessionTileRequest, _: None = Depends(require_api_key)):
    """Ingest a session summary as a searchable ISMA_Quantum tile.

    Generates embedding, computes content_hash, and stores in Weaviate.
    Returns the tile UUID for reference.
    """
    import requests
    from datetime import datetime, timezone
    from isma.config import WEAVIATE_URL, EMBEDDING_URL

    if req.platform not in ALLOWED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Invalid platform: {req.platform}")
    if req.source_type not in ALLOWED_SESSION_SOURCE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid source_type: {req.source_type}")
    if req.truth_tier not in ALLOWED_TRUTH_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid truth_tier: {req.truth_tier}")
    if req.scale not in ALLOWED_TILE_SCALES:
        raise HTTPException(status_code=400, detail=f"Invalid scale: {req.scale}")

    canonical_content = canonicalize_text(req.content)
    content_hash = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()[:16]
    now_iso = datetime.now(timezone.utc).isoformat()
    tile_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{content_hash}/{req.scale}/session"))

    # Generate embedding
    try:
        emb_resp = requests.post(
            EMBEDDING_URL,
            json={"input": canonical_content[:8000], "model": "Qwen/Qwen3-Embedding-8B"},
            timeout=30,
        )
        emb_resp.raise_for_status()
        vector = emb_resp.json()["data"][0]["embedding"]
    except Exception as e:
        logging.error(f"Session ingest: embedding failed: {e}")
        raise HTTPException(status_code=502, detail=f"Embedding failed: {e}")

    # Auto-generate rosetta if not provided (first 200 chars)
    rosetta = req.rosetta_summary or canonical_content[:200].replace("\n", " ").strip()

    # Build Weaviate object
    tile_obj = {
        "class": "ISMA_Quantum",
        "id": tile_uuid,
        "properties": {
            "content": canonical_content,
            "content_hash": content_hash,
            "source_file": req.source_file,
            "source_type": req.source_type,
            "platform": req.platform,
            "actor": req.actor,
            "scale": req.scale,
            "token_count": len(canonical_content) // 4,
            "start_char": 0,
            "end_char": len(canonical_content),
            "timestamp": now_iso,
            "loaded_at": now_iso,
            "rosetta_summary": rosetta,
            "truth_tier": req.truth_tier,
            "hmm_enriched": False,
            # memory-governance fields, stamped at ingest so session tiles are
            # eligible under the read-side validity filter and carry provenance
            # (this is a tile-write path parallel to isma_core._embed_to_weaviate;
            # both must stamp these or the default is_superseded filter drops them).
            "valid_from": now_iso,
            "superseded_by": "",
            "invalidated_at": "",
            "is_superseded": False,
            "lineage_root": content_hash,
            "provenance_hash": json.dumps(
                {"source": req.source_file, "content_hash": content_hash, "timestamp": now_iso},
                sort_keys=True,
            ),
        },
        "vector": vector,
    }

    if req.session_id:
        tile_obj["properties"]["session_id"] = req.session_id
    if req.tags:
        tile_obj["properties"]["dominant_motifs"] = req.tags

    # Store in Weaviate
    try:
        w_resp = requests.post(
            f"{WEAVIATE_URL}/v1/objects",
            json=tile_obj,
            timeout=30,
        )
        if w_resp.status_code == 422 and "already exists" in w_resp.text.lower():
            logging.info(f"Session tile already exists: {content_hash} ({req.source_file}) -> {tile_uuid}")
            return {
                "success": True,
                "duplicate": True,
                "tile_id": tile_uuid,
                "content_hash": content_hash,
                "source_file": req.source_file,
                "token_count": tile_obj["properties"]["token_count"],
            }
        if w_resp.status_code not in (200, 201):
            raise HTTPException(
                status_code=502,
                detail=f"Weaviate store failed: {w_resp.status_code} {w_resp.text[:200]}",
            )
        tile_id = w_resp.json().get("id", tile_uuid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weaviate store failed: {e}")

    logging.info(f"Session tile ingested: {content_hash} ({req.source_file}) -> {tile_id}")

    return {
        "success": True,
        "tile_id": tile_id,
        "content_hash": content_hash,
        "source_file": req.source_file,
        "token_count": tile_obj["properties"]["token_count"],
    }


@app.post("/ingest/session/batch")
def ingest_session_batch(
    tiles: List[SessionTileRequest],
    _: None = Depends(require_api_key),
):
    """Ingest multiple session tiles in one request."""
    if len(tiles) > 500:
        raise HTTPException(status_code=400, detail="batch size must be <= 500")
    results = []
    for tile in tiles:
        try:
            result = ingest_session_tile(tile)
            results.append(result)
        except HTTPException as e:
            results.append({"success": False, "error": e.detail, "source_file": tile.source_file})

    succeeded = sum(1 for r in results if r.get("success"))
    return {
        "total": len(tiles),
        "succeeded": succeeded,
        "failed": len(tiles) - succeeded,
        "results": results,
    }
