#!/usr/bin/env python3
"""
HMM Package Builder — On-demand package creation for AI platform enrichment.

Reads the theme search index, selects unenriched items that fit a platform's
token budget, fetches full content from Weaviate, and writes a markdown package
file ready for submission to an AI platform.

Usage:
    python3 hmm_package_builder.py next --platform chatgpt   # Build next package
    python3 hmm_package_builder.py complete                   # Mark current done
    python3 hmm_package_builder.py fail <reason>              # Requeue current
    python3 hmm_package_builder.py stats                      # Show progress
    python3 hmm_package_builder.py reset                      # Clear all state

Designed to be called by Claude Code workers on worker nodes.
"""

import sys
from isma.config import WEAVIATE_URL as CONFIG_WEAVIATE_URL, NEO4J_URI as CONFIG_NEO4J_URI, REDIS_HOST as CONFIG_REDIS_HOST, REDIS_PORT as CONFIG_REDIS_PORT, EMBEDDING_URL as CONFIG_EMBEDDING_URL, ISMA_QUERY_API as CONFIG_ISMA_QUERY_API, NIGHTLY_MAC_HOST as CONFIG_NIGHTLY_MAC_HOST
from isma.config import ISMA_THEME_INDEX_PATH as CONFIG_THEME_INDEX_PATH, ISMA_HMM_PKG_STATE_PATH as CONFIG_HMM_PKG_STATE_PATH, ISMA_HMM_PKG_DIR as CONFIG_HMM_PKG_DIR
import os
import json
import time
import logging
import argparse
import hashlib
import requests
import redis
from isma.src.hmm.motifs import V0_MOTIFS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pkg_builder")

# ============================================================================
# Configuration
# ============================================================================

WEAVIATE_URL = CONFIG_WEAVIATE_URL
WEAVIATE_GQL = f"{WEAVIATE_URL}/v1/graphql"
WEAVIATE_REST = f"{WEAVIATE_URL}/v1"
WEAVIATE_CLASS = "ISMA_Quantum"
REDIS_HOST = CONFIG_REDIS_HOST
REDIS_PORT = int(str(CONFIG_REDIS_PORT))

INDEX_PATH = CONFIG_THEME_INDEX_PATH
PKG_DIR = CONFIG_HMM_PKG_DIR
STATE_PATH = CONFIG_HMM_PKG_STATE_PATH

# Redis key prefix for package tracking
PFX = "hmm:pkg:"

# Platform token budgets (usable content, leaving room for prompt + response)
# TARGET: 75-100K tokens consistently across ALL platforms.
# Larger budgets cause AI to truncate JSON output or timeout.
PLATFORM_BUDGETS = {
    "chatgpt": 75_000,   # GPT-4o: 128K context, 75K input leaves room for response
    "claude": 100_000,   # Claude: 200K context, reserve 100K for response
    "perplexity": 80_000,
    "grok": 75_000,      # Grok: 128K+ context, 75K input
    "gemini": 100_000,   # Gemini 3.1 Pro: 200K context
}

CHARS_PER_TOKEN = 3.8  # Conservative: tiktoken audit shows actual avg is 3.81

# How many items per package — CAPPED to ensure AI completes ALL items
# Large packages (90+) cause 44% failure rate: AIs truncate JSON mid-stream
# Token budget is the PRIMARY constraint; item cap is a safety net only.
MIN_ITEMS_PER_PKG = 3
MAX_ITEMS_PER_PKG = 60  # Safety cap — token budget should hit first

# Platform-specific max items (overrides MAX_ITEMS_PER_PKG)
# Set high so TOKEN BUDGET is the binding constraint (75-100K tokens).
# With avg ~2-3K tokens/item, 75K budget ≈ 25-37 items — well under 60.
PLATFORM_MAX_ITEMS = {
    "chatgpt": 40,    # Let token budget (50K) be the binding constraint
    "claude": 50,
    "gemini": 30,     # Reduced from 60: storage timeout at 60 items (embedding API ~5-10s/item)
    "grok": 40,       # Let token budget (50K) be the binding constraint
    "perplexity": 40, # 80K budget
}

# Anchors per package (kernel/layer seeds for context grounding)
MAX_ANCHORS = 10

# Tile-group splitting: large items (many full_4096 tiles) get split into groups
# so each group gets its own enrichment analysis instead of one averaged analysis
TILE_GROUP_SIZE = 5  # Max full_4096 tiles per group

# Max tile groups per content_hash in a single package.
# Items with more groups than this are deferred to pass 2 (after small items).
# Set high so large documents fill the TOKEN BUDGET — the budget is the real cap.
MAX_GROUPS_PER_HASH = 20

# Source file patterns to SKIP — these don't need enrichment
# Heartbeat files are 3-line status pings, not meaningful content
SKIP_SOURCE_PATTERNS = ["heartbeat"]

# Batch size for OR-filter Weaviate tile fetches.
WEAVIATE_TILE_BATCH_SIZE = 25

# ============================================================================
# Connections
# ============================================================================

_redis: redis.Redis = None
_wv_session = requests.Session()


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return _redis


def weaviate_gql(query: str, timeout: int = 120) -> dict:
    """Execute Weaviate GraphQL query. Returns data dict or empty dict."""
    try:
        r = _wv_session.post(f"{WEAVIATE_URL}/v1/graphql", json={"query": query}, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if data.get("errors"):
                log.error(f"GraphQL errors: {json.dumps(data['errors'])[:300]}")
                return {}
            return data.get("data", {})
    except Exception as e:
        log.error(f"Weaviate error: {e}")
    return {}


# Instance ID for multi-worker package isolation
# Each worker gets its own "current package" tracking via hostname
import socket
_INSTANCE_ID = os.environ.get("TAEY_NODE_ID", socket.gethostname().split("-")[0])

# ============================================================================
# Theme Index & State Management
# ============================================================================

def load_index() -> dict:
    """Load theme search index."""
    with open(INDEX_PATH) as f:
        return json.load(f)


def is_item_available(content_hash: str) -> bool:
    """Check if item is not in-progress or completed."""
    r = get_redis()
    return (not r.exists(f"{PFX}in_progress:{content_hash}")
            and not r.sismember(f"{PFX}completed", content_hash))


def batch_check_available(content_hashes: list) -> set:
    """Batch check which items are available using Redis pipeline."""
    r = get_redis()
    # Get completed set once
    completed = r.smembers(f"{PFX}completed")
    # Batch check in-progress keys
    pipe = r.pipeline()
    for ch in content_hashes:
        pipe.exists(f"{PFX}in_progress:{ch}")
    in_progress_flags = pipe.execute()

    available = set()
    for ch, is_in_progress in zip(content_hashes, in_progress_flags):
        if not is_in_progress and ch not in completed:
            available.add(ch)
    return available


def mark_in_progress(content_hashes: list, platform: str, pkg_id: str):
    """Mark items as in-progress for a platform."""
    r = get_redis()
    pipe = r.pipeline()
    for ch in content_hashes:
        pipe.set(f"{PFX}in_progress:{ch}", f"{platform}:{pkg_id}", ex=900)  # 15min TTL
    pipe.execute()


def mark_completed(content_hashes: list, pkg_id: str = ""):
    """Mark items as completed with ownership verification.

    If pkg_id is provided, only marks items whose in-progress key
    matches this package. Prevents TTL race condition where an expired
    item gets re-assigned to a new package but the old one completes it.
    """
    r = get_redis()
    verified_hashes = []
    if pkg_id and content_hashes:
        # Ownership check: only complete items still owned by this package
        for ch in content_hashes:
            owner = r.get(f"{PFX}in_progress:{ch}")
            if owner is None or pkg_id in owner:
                verified_hashes.append(ch)
            else:
                log.warning(f"  Ownership mismatch for {ch[:12]}: owned by {owner}, not {pkg_id} — skipping")
    else:
        verified_hashes = content_hashes

    if verified_hashes:
        r.sadd(f"{PFX}completed", *verified_hashes)
    # Clean up in-progress keys
    pipe = r.pipeline()
    for ch in verified_hashes:
        pipe.delete(f"{PFX}in_progress:{ch}")
    pipe.execute()
    return len(verified_hashes)


def get_current_package(platform: str = None) -> dict:
    """Get the current package being worked on by THIS instance."""
    r = get_redis()
    if platform:
        data = r.get(f"{PFX}current:{_INSTANCE_ID}:{platform}")
    else:
        # Find any current package for this instance
        for p in PLATFORM_BUDGETS:
            data = r.get(f"{PFX}current:{_INSTANCE_ID}:{p}")
            if data:
                return json.loads(data)
        return {}
    return json.loads(data) if data else {}


def set_current_package(platform: str, pkg_info: dict):
    """Set the current package for a platform on THIS instance."""
    r = get_redis()
    r.set(f"{PFX}current:{_INSTANCE_ID}:{platform}", json.dumps(pkg_info), ex=3600)


def clear_current_package(platform: str):
    """Clear the current package for a platform on THIS instance."""
    r = get_redis()
    r.delete(f"{PFX}current:{_INSTANCE_ID}:{platform}")


# ============================================================================
# Content Fetching
# ============================================================================

def fetch_full_content(content_hash: str) -> str:
    """Fetch and reconstruct full content for a content_hash from Weaviate.

    Gets all full_4096 tiles, or falls back to context_2048 then search_512
    when larger tiles are unavailable, then de-overlaps using start_char/end_char
    to reconstruct the original content without duplication.
    """
    tiles = _fetch_tiles_with_scale_fallback(content_hash, limit=100)
    if not tiles:
        return ""

    # Sort by tile_index
    tiles.sort(key=lambda t: t.get("tile_index", 0))

    # De-overlap: reconstruct using start_char/end_char
    result_parts = []
    covered_up_to = 0

    for t in tiles:
        content = t.get("content", "")
        start = t.get("start_char", 0) or 0
        end = t.get("end_char", start + len(content))

        if start >= covered_up_to:
            # No overlap — take full content
            result_parts.append(content)
        elif end > covered_up_to:
            # Partial overlap — skip the overlapping prefix
            skip_chars = covered_up_to - start
            if skip_chars < len(content):
                result_parts.append(content[skip_chars:])

        covered_up_to = max(covered_up_to, end)

    return "".join(result_parts)


def _deoverlap_tiles(tiles: list) -> str:
    """De-overlap a sorted list of tiles using start_char/end_char."""
    result_parts = []
    covered_up_to = 0
    for t in tiles:
        content = t.get("content", "")
        start = t.get("start_char", 0) or 0
        end = t.get("end_char", start + len(content))
        if start >= covered_up_to:
            result_parts.append(content)
        elif end > covered_up_to:
            skip_chars = covered_up_to - start
            if skip_chars < len(content):
                result_parts.append(content[skip_chars:])
        covered_up_to = max(covered_up_to, end)
    return "".join(result_parts)


def _escape_gql_value(value: str) -> str:
    """Escape a string for embedding in a GraphQL value literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_doc_hash_filter(doc_hash: str) -> str:
    """Match doc_hash with legacy checksum/content_hash fallbacks."""
    safe_hash = _escape_gql_value(doc_hash)
    return (
        "{ operator: Or, operands: ["
        f'{{ path: ["doc_hash"], operator: Equal, valueText: "{safe_hash}" }}, '
        f'{{ path: ["checksum"], operator: Equal, valueText: "{safe_hash}" }}, '
        f'{{ path: ["content_hash"], operator: Equal, valueText: "{safe_hash}" }}'
        "] }"
    )


def _batched(items: list, batch_size: int):
    """Yield fixed-size batches from a list."""
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def _build_hash_where(field_name: str, content_hashes: list, scale: str) -> str:
    """Build a WHERE clause for a batch of content hashes at one scale."""
    if len(content_hashes) == 1:
        hash_filter = (
            f'{{ path: ["{field_name}"], operator: Equal, '
            f'valueText: "{_escape_gql_value(content_hashes[0])}" }}'
        )
    else:
        hash_operands = ", ".join(
            f'{{ path: ["{field_name}"], operator: Equal, '
            f'valueText: "{_escape_gql_value(ch)}" }}'
            for ch in content_hashes
        )
        hash_filter = f"{{ operator: Or, operands: [{hash_operands}] }}"

    return (
        "{ operator: And, operands: ["
        f'{hash_filter}, '
        f'{{ path: ["scale"], operator: Equal, valueText: "{scale}" }}'
        "] }"
    )


def _fetch_tiles_with_scale_fallback_batch(content_hashes: list, limit: int) -> dict:
    """Fetch tiles for many content hashes using scale fallback in batches."""
    unique_hashes = list(dict.fromkeys(ch for ch in content_hashes if ch))
    tiles_by_hash = {ch: [] for ch in unique_hashes}
    if not unique_hashes:
        return tiles_by_hash

    fields = (
        "doc_hash content_hash checksum scale tile_index start_char end_char content "
        "token_count source_file source_type platform session_id exchange_index"
    )

    for field_name in ("doc_hash", "checksum", "content_hash"):
        for scale in ("full_4096", "context_2048", "search_512"):
            unresolved = [ch for ch in unique_hashes if not tiles_by_hash[ch]]
            if not unresolved:
                return tiles_by_hash

            for batch_hashes in _batched(unresolved, WEAVIATE_TILE_BATCH_SIZE):
                where_clause = _build_hash_where(field_name, batch_hashes, scale)
                q = f"""{{
                    Get {{
                        {WEAVIATE_CLASS}(
                            where: {where_clause}
                            limit: {max(limit * len(batch_hashes), len(batch_hashes))}
                        ) {{
                            {fields}
                        }}
                    }}
                }}"""
                data = weaviate_gql(q)
                tiles = data.get("Get", {}).get(WEAVIATE_CLASS, [])
                if not tiles:
                    continue

                grouped = {}
                for tile in tiles:
                    group_key = tile.get(field_name)
                    if group_key in batch_hashes:
                        grouped.setdefault(group_key, []).append(tile)

                for ch, grouped_tiles in grouped.items():
                    if grouped_tiles and not tiles_by_hash[ch]:
                        tiles_by_hash[ch] = grouped_tiles[:limit]

    return tiles_by_hash


def _fetch_tiles_with_scale_fallback(content_hash: str, limit: int) -> list:
    """Fetch tiles for a content hash using the largest available scale."""
    return _fetch_tiles_with_scale_fallback_batch([content_hash], limit).get(content_hash, [])


def _tile_metadata_from_tiles(content_hash: str, tiles: list) -> dict:
    """Extract item metadata from the first tile in a fetched batch."""
    if not tiles:
        return {}
    tile = tiles[0]
    return {
        "content_hash": tile.get("doc_hash") or tile.get("checksum") or tile.get("content_hash", content_hash),
        "source_file": tile.get("source_file", ""),
        "source_type": tile.get("source_type", ""),
        "platform": tile.get("platform", ""),
        "session_id": tile.get("session_id", ""),
        "exchange_index": tile.get("exchange_index"),
        "token_count": tile.get("token_count", 0),
        "scale": tile.get("scale", ""),
    }


def _build_tile_groups_from_tiles(content_hash: str, tiles: list) -> list:
    """Split a fetched tile list into one or more de-overlapped groups."""
    if not tiles:
        return []

    tiles = sorted(tiles, key=lambda t: t.get("tile_index", 0))
    selected_scale = tiles[0].get("scale", "")

    if len(tiles) <= TILE_GROUP_SIZE:
        content = _deoverlap_tiles(tiles)
        tokens = int(len(content) / CHARS_PER_TOKEN)
        return [{"group_id": content_hash, "content": content,
                 "token_count": tokens, "tile_range": None}]

    groups = []
    for i in range(0, len(tiles), TILE_GROUP_SIZE):
        group_tiles = tiles[i:i + TILE_GROUP_SIZE]
        start_idx = group_tiles[0].get("tile_index", i)
        end_idx = group_tiles[-1].get("tile_index", i + len(group_tiles) - 1)
        content = _deoverlap_tiles(group_tiles)
        tokens = int(len(content) / CHARS_PER_TOKEN)
        group_id = f"{content_hash}@{start_idx}-{end_idx}"
        groups.append({"group_id": group_id, "content": content,
                       "token_count": tokens, "tile_range": (start_idx, end_idx)})

    log.info(f"  Split {content_hash[:12]} into {len(groups)} tile groups "
             f"({len(tiles)} {selected_scale or 'fallback'} tiles, {TILE_GROUP_SIZE}/group)")
    return groups


def fetch_tile_groups_batch(content_hashes: list, limit: int = 500) -> tuple:
    """Batch-fetch tiles, then derive tile groups and metadata per content hash."""
    tiles_by_hash = _fetch_tiles_with_scale_fallback_batch(content_hashes, limit=limit)
    groups_by_hash = {}
    metadata_by_hash = {}
    for ch in content_hashes:
        tiles = tiles_by_hash.get(ch, [])
        groups_by_hash[ch] = _build_tile_groups_from_tiles(ch, tiles)
        metadata_by_hash[ch] = _tile_metadata_from_tiles(ch, tiles)
    return groups_by_hash, metadata_by_hash


def fetch_tile_groups(content_hash: str) -> list:
    """Fetch tiles with scale fallback and split large items.

    Returns list of dicts:
        group_id: content_hash (small items) or content_hash@start-end (groups)
        content: de-overlapped text for this group
        token_count: estimated tokens
        tile_range: (start_idx, end_idx) or None for single-group items
    """
    return _build_tile_groups_from_tiles(
        content_hash,
        _fetch_tiles_with_scale_fallback(content_hash, limit=500),
    )


def get_item_metadata(content_hash: str) -> dict:
    """Get metadata for a content_hash (source_file, platform, session_id, etc)."""
    q = f"""{{
        Get {{
            {WEAVIATE_CLASS}(
                where: {{
                    operator: And,
                    operands: [
                        {_build_doc_hash_filter(content_hash)},
                        {{ path: ["scale"], operator: Equal, valueText: "search_512" }}
                    ]
                }}
                limit: 1
            ) {{
                source_file source_type platform session_id exchange_index
                doc_hash content_hash token_count
            }}
        }}
    }}"""

    data = weaviate_gql(q)
    tiles = data.get("Get", {}).get(WEAVIATE_CLASS, [])
    if not tiles:
        return {}
    tile = tiles[0]
    tile["content_hash"] = tile.get("doc_hash") or tile.get("content_hash", content_hash)
    return tile


def _should_skip_source(source_file: str) -> bool:
    """Check if a source file matches skip patterns (heartbeats, etc)."""
    if not source_file:
        return False
    sf_lower = source_file.lower()
    return any(pat in sf_lower for pat in SKIP_SOURCE_PATTERNS)


def estimate_full_tokens(content_hash: str) -> int:
    """Get actual token count for a content_hash from full_4096 tiles."""
    q = f"""{{
        Aggregate {{
            {WEAVIATE_CLASS}(
                where: {{
                    operator: And,
                    operands: [
                        {_build_doc_hash_filter(content_hash)},
                        {{ path: ["scale"], operator: Equal, valueText: "full_4096" }}
                    ]
                }}
            ) {{
                token_count {{ sum }}
                meta {{ count }}
            }}
        }}
    }}"""

    data = weaviate_gql(q)
    agg = data.get("Aggregate", {}).get(WEAVIATE_CLASS, [{}])
    if agg:
        tile_count = agg[0].get("meta", {}).get("count", 0)
        token_sum = agg[0].get("token_count", {}).get("sum", 0)
        if tile_count and token_sum:
            # Account for phi-tiling overlap: actual tokens ≈ sum / (overlap_factor)
            # With step_size=1507 and chunk_size=4096: overlap ≈ 63%, so unique ≈ 37%
            # But for single-tile items, no overlap
            if tile_count == 1:
                return int(token_sum)
            # For multi-tile: unique content ≈ first_tile + (n-1) * step_size_tokens
            # step_size=1507 chars ≈ 377 tokens
            step_tokens = int(1507 / CHARS_PER_TOKEN)
            return int(agg[0].get("token_count", {}).get("sum", 0) / tile_count) + (tile_count - 1) * step_tokens
    return 0


# ============================================================================
# Direct Weaviate Fallback (for tiles not captured by theme index)
# ============================================================================

# Weaviate cursor pagination — persisted in Redis so multiple workers
# resume from where they left off
_WEAVIATE_CURSOR_KEY = f"{PFX}weaviate_sweep_cursor"
_WEAVIATE_SWEEP_PAGE = 10000  # tiles per page (cursor has no offset limit)


_TRANSCRIPT_PLATFORMS = ["claude", "claude_chat", "chatgpt", "gemini", "grok"]
_SWEEP_CURSOR_TRANSCRIPT_KEY = f"{PFX}weaviate_sweep_cursor_transcript"
_CHRONO_QUEUE_KEY = f"{PFX}chrono_queue"


def _sweep_chrono_transcripts(n: int = 500, seen_hashes: set = None) -> list:
    """Pull transcript files from chronological Redis queue, resolve content hashes.

    Pops files oldest-first from hmm:pkg:chrono_queue, looks up their
    search_512 tiles in Weaviate, returns unenriched items in chronological order.
    """
    r = get_redis()
    seen = seen_hashes or set()
    completed = r.smembers(f"{PFX}completed")
    results = []
    files_checked = 0
    max_pops = 200  # Don't pop too many in one call

    while len(results) < n and files_checked < max_pops:
        entry = r.lpop(_CHRONO_QUEUE_KEY)
        if not entry:
            log.info("Chrono queue empty")
            break
        files_checked += 1
        try:
            info = json.loads(entry)
        except json.JSONDecodeError:
            continue

        source_file = info.get("file", "")
        platform = info.get("platform", "")
        if not source_file:
            continue

        # Look up tiles for this source file in Weaviate
        escaped = source_file.replace('"', '\\"')
        q = f"""{{
            Get {{
                {WEAVIATE_CLASS}(
                    where: {{
                        operator: And,
                        operands: [
                            {{ path: ["source_file"], operator: Equal, valueText: "{escaped}" }},
                            {{ path: ["scale"], operator: Equal, valueText: "search_512" }},
                            {{ path: ["hmm_enriched"], operator: NotEqual, valueBoolean: true }}
                        ]
                    }}
                    limit: 50
                ) {{
                    doc_hash content_hash source_file source_type token_count
                    _additional {{ id }}
                }}
            }}
        }}"""

        data = weaviate_gql(q, timeout=30)
        tiles = data.get("Get", {}).get(WEAVIATE_CLASS, [])

        for t in tiles:
            ch = t.get("doc_hash") or t.get("content_hash")
            if not ch or ch in seen or ch in completed:
                continue
            seen.add(ch)
            results.append({
                "content_hash": ch,
                "source_file": t.get("source_file", ""),
                "source_type": t.get("source_type", "transcript"),
                "score": 0.0,
                "token_estimate": t.get("token_count", 0) or 0,
            })
            if len(results) >= n:
                break

    log.info(f"Chrono sweep: {len(results)} items from {files_checked} files (queue remaining: {r.llen(_CHRONO_QUEUE_KEY)})")
    return results


def _sweep_unenriched_batch(n: int = 200, seen_hashes: set = None) -> list:
    """Find unenriched content_hashes using efficient server-side Weaviate filtering.

    Uses WHERE clause (hmm_enriched=false, scale=search_512) for server-side
    filtering — avoids scanning all 1M+ tiles. Much faster than the old
    cursor-based full scan with client-side filtering.

    Returns list of dicts: {content_hash, source_file, source_type, token_estimate}
    """
    r = get_redis()
    seen = seen_hashes or set()
    completed = r.smembers(f"{PFX}completed")

    # Server-side filter: only unenriched search_512 tiles
    fetch_limit = min(n * 15, 5000)
    q = f"""{{
        Get {{
            {WEAVIATE_CLASS}(
                where: {{
                    operator: And,
                    operands: [
                        {{ path: ["hmm_enriched"], operator: NotEqual, valueBoolean: true }},
                        {{ path: ["scale"], operator: Equal, valueText: "search_512" }}
                    ]
                }}
                limit: {fetch_limit}
            ) {{
                doc_hash content_hash source_file source_type token_count
            }}
        }}
    }}"""

    data = weaviate_gql(q, timeout=120)
    tiles = data.get("Get", {}).get(WEAVIATE_CLASS, [])
    if not tiles:
        log.info("Unenriched batch: no tiles found from Weaviate")
        return []

    # Deduplicate by content_hash, keeping first occurrence
    candidates = {}
    for t in tiles:
        ch = t.get("doc_hash") or t.get("content_hash")
        if not ch or ch in seen or ch in completed:
            continue
        if ch not in candidates:
            candidates[ch] = t

    if not candidates:
        log.info(f"Unenriched batch: {len(tiles)} tiles but all completed/seen")
        return []

    # Batch check Redis availability (not in_progress, not completed)
    available = batch_check_available(list(candidates.keys()))

    results = []
    for ch in candidates:
        if ch not in available:
            continue
        t = candidates[ch]
        source_file = t.get("source_file", "")
        if _should_skip_source(source_file):
            continue
        results.append({
            "content_hash": ch,
            "source_file": source_file,
            "source_type": t.get("source_type", ""),
            "score": 0.0,
            "token_estimate": t.get("token_count", 0) or 0,
        })
        seen.add(ch)
        if len(results) >= n:
            break

    log.info(f"Unenriched batch: {len(results)} available from {len(tiles)} tiles "
             f"({len(candidates)} unique hashes)")
    return results


def _sweep_weaviate_direct(n: int = 2000, seen_hashes: set = None,
                           platforms: list = None) -> list:
    """Legacy cursor-based sweep — fallback when batch query finds nothing.

    Paginate ISMA_Quantum with cursor-based pagination (after + limit).
    Filters client-side since Weaviate cursor API doesn't support
    where + after together.

    Args:
        platforms: If set, only include tiles from these platforms.

    Returns list of dicts: {content_hash, source_file, source_type, token_estimate}
    """
    r = get_redis()
    seen = seen_hashes or set()
    completed = r.smembers(f"{PFX}completed")
    platform_set = set(platforms) if platforms else None

    results = []
    cursor_key = _SWEEP_CURSOR_TRANSCRIPT_KEY if platforms else _WEAVIATE_CURSOR_KEY
    cursor = r.get(cursor_key) or None
    scanned = 0
    max_scan = 1_100_000  # safety cap — slightly above total tile count

    while len(results) < n and scanned < max_scan:
        after_clause = f', after: "{cursor}"' if cursor else ""
        q = f"""{{
            Get {{
                {WEAVIATE_CLASS}(
                    limit: {_WEAVIATE_SWEEP_PAGE}{after_clause}
                ) {{
                    doc_hash content_hash source_file source_type token_count
                    platform scale hmm_enriched
                    _additional {{ id }}
                }}
            }}
        }}"""

        data = weaviate_gql(q, timeout=120)
        tiles = data.get("Get", {}).get(WEAVIATE_CLASS, [])
        if not tiles:
            # Reached end of dataset — reset cursor and wrap around once
            log.info(f"Sweep: reached end after scanning {scanned} objects, resetting cursor")
            r.delete(cursor_key)
            if cursor:  # Had a cursor = was mid-scan, wrap to start
                cursor = None
                continue  # Retry from beginning
            break  # Already started from beginning, truly empty

        cursor = tiles[-1]["_additional"]["id"]
        scanned += len(tiles)

        # Client-side filtering: scale=search_512, hmm_enriched=false, platform match
        candidates = {}
        for t in tiles:
            ch = t.get("doc_hash") or t.get("content_hash")
            if not ch or ch in seen or ch in completed:
                continue
            if t.get("scale") != "search_512":
                continue
            if t.get("hmm_enriched"):
                continue
            if platform_set and t.get("platform") not in platform_set:
                continue
            if ch not in candidates:
                candidates[ch] = t

        if candidates:
            available = batch_check_available(list(candidates.keys()))
            for ch in candidates:
                if ch in available:
                    t = candidates[ch]
                    results.append({
                        "content_hash": ch,
                        "source_file": t.get("source_file", ""),
                        "source_type": t.get("source_type", ""),
                        "score": 0.0,
                        "token_estimate": t.get("token_count", 0) or 0,
                    })
                    seen.add(ch)
                    if len(results) >= n:
                        break

        if len(tiles) < _WEAVIATE_SWEEP_PAGE:
            break  # last page

    # Persist updated cursor
    if cursor:
        r.set(cursor_key, cursor)
    log.info(f"Sweep: found {len(results)} items after scanning {scanned} objects")
    return results


# ============================================================================
# Package Building
# ============================================================================

def select_theme(exclude_ids: set = None) -> tuple:
    """Select the next theme to work on. Returns (theme_key, theme_data) or (None, None).

    Samples 50 items per theme to estimate availability. Fast even over network.

    Args:
        exclude_ids: Theme IDs to skip (already tried in this package build).
    """
    index = load_index()
    best_key = None
    best_available = 0
    exclude = exclude_ids or set()

    for key, theme in index.items():
        if theme.get("theme_id") in exclude:
            continue

        all_items = theme.get("unenriched_corpus", []) + theme.get("unenriched_exchanges", [])
        if not all_items:
            continue

        # Sample up to 50 items for a fast availability check (random to avoid bias)
        import random
        sample = random.sample(all_items, min(50, len(all_items)))
        sample_hashes = [it["content_hash"] for it in sample]
        available = batch_check_available(sample_hashes)

        # Estimate total available from sample ratio
        ratio = len(available) / len(sample)
        estimated_available = int(ratio * len(all_items))

        if estimated_available > best_available:
            best_available = estimated_available
            best_key = key

    if best_key:
        return best_key, index[best_key]
    return None, None


def _gather_candidates_from_theme(theme_data: dict, seen_hashes: set) -> list:
    """Get available candidates from a theme, excluding already-seen hashes."""
    import random as _rng
    corpus_all = theme_data.get("unenriched_corpus", [])
    exchange_all = theme_data.get("unenriched_exchanges", [])
    _rng.shuffle(corpus_all)
    _rng.shuffle(exchange_all)
    corpus_raw = corpus_all[:1000]
    exchange_raw = exchange_all[:1000]

    # Filter out already-seen items before checking Redis
    new_hashes = [it["content_hash"] for it in corpus_raw + exchange_raw
                  if it["content_hash"] not in seen_hashes]
    if not new_hashes:
        return []

    available_set = batch_check_available(new_hashes)
    corpus_items = [it for it in corpus_raw
                    if it["content_hash"] in available_set and it["content_hash"] not in seen_hashes]
    exchange_items = [it for it in exchange_raw
                      if it["content_hash"] in available_set and it["content_hash"] not in seen_hashes]

    # Interleave corpus and exchanges — 2 corpus : 1 exchange ratio
    candidates = []
    ci, ei = 0, 0
    while ci < len(corpus_items) or ei < len(exchange_items):
        if ci < len(corpus_items):
            candidates.append(("CORPUS", corpus_items[ci]))
            ci += 1
        if ci < len(corpus_items):
            candidates.append(("CORPUS", corpus_items[ci]))
            ci += 1
        if ei < len(exchange_items):
            candidates.append(("TRANSCRIPT", exchange_items[ei]))
            ei += 1
    return candidates


def build_package(platform: str) -> str:
    """Build next package for a platform. Returns path to markdown file.

    Pulls from multiple themes to fill the token budget. Stops when
    budget is full or no more items are available across all themes.
    """
    budget_tokens = PLATFORM_BUDGETS.get(platform)
    if not budget_tokens:
        log.error(f"Unknown platform: {platform}")
        return ""

    # Reserve tokens for prompt (~2K) and anchors (~1K)
    content_budget = budget_tokens - 3000

    # Collect items across themes until budget is filled
    pkg_items = []
    actual_tokens = 0
    seen_hashes = set()
    themes_used = []
    tried_themes = set()
    primary_theme_data = None

    used_sweep = False  # Track whether we've fallen back to direct sweep

    def _load_groups_and_metadata(batch_hashes: list, limit: int = 500) -> tuple:
        """Batch-fetch tile groups and metadata for a list of hashes."""
        return fetch_tile_groups_batch(batch_hashes, limit=limit)

    while actual_tokens < content_budget * 0.8:  # Keep filling until 80%+ of budget
        max_items_limit = PLATFORM_MAX_ITEMS.get(platform, MAX_ITEMS_PER_PKG)
        if len(pkg_items) >= max_items_limit:
            break
        theme_key, theme_data = select_theme(exclude_ids=tried_themes)
        if not theme_data:
            # Theme index exhausted — try efficient batch query, then chrono, then cursor sweep
            if not used_sweep:
                # Primary: server-side filtered batch query (fast — WHERE hmm_enriched=false)
                sweep_items = _sweep_unenriched_batch(n=200, seen_hashes=seen_hashes)
                if sweep_items:
                    log.info(f"Using efficient unenriched batch ({len(sweep_items)} hashes)")
                else:
                    # Fallback 1: chronological transcript queue
                    chrono_items = _sweep_chrono_transcripts(n=500, seen_hashes=seen_hashes)
                    if chrono_items:
                        sweep_items = chrono_items
                        log.info(f"Using chronological transcript queue ({len(chrono_items)} items)")
                    else:
                        # Fallback 2: legacy cursor-based full scan
                        log.info("Batch + chrono empty — falling back to cursor-based sweep")
                        sweep_items = _sweep_weaviate_direct(
                            n=500, seen_hashes=seen_hashes,
                        )
                if sweep_items:
                    used_sweep = True
                    # Use a synthetic theme_data for sweep items
                    primary_theme_data = primary_theme_data or {
                        "theme_id": "sweep",
                        "display_name": "Direct Sweep",
                        "description": "Tiles not captured by thematic nearVector search",
                        "required_motifs": [],
                        "supporting_motifs": [],
                        "anchor_rosettas": [],
                    }
                    themes_used.append("sweep (direct)")
                    added_sweep = 0
                    max_items = PLATFORM_MAX_ITEMS.get(platform, MAX_ITEMS_PER_PKG)

                    def _add_groups_to_pkg(item, groups, meta_dict):
                        nonlocal actual_tokens, added_sweep
                        ch = item["content_hash"]
                        src_type = item.get("source_type", meta_dict.get("source_type", ""))
                        item_type = "TRANSCRIPT" if src_type == "transcript" else "CORPUS"
                        source_file = item.get("source_file", meta_dict.get("source_file", ""))
                        for group in groups:
                            remaining = content_budget - actual_tokens
                            if group["token_count"] > remaining and len(pkg_items) >= 1:
                                break
                            pkg_items.append({
                                "type": item_type,
                                "content_hash": ch,
                                "group_id": group["group_id"],
                                "tile_range": group["tile_range"],
                                "source_file": source_file,
                                "platform": meta_dict.get("platform", ""),
                                "session_id": meta_dict.get("session_id", ""),
                                "exchange_index": meta_dict.get("exchange_index"),
                                "content": group["content"],
                                "token_count": group["token_count"],
                                "score": 0.0,
                            })
                            actual_tokens += group["token_count"]
                            added_sweep += 1
                            if len(pkg_items) >= max_items or actual_tokens >= content_budget:
                                break

                    def _process_sweep_batch(batch_items: list) -> bool:
                        """Fetch sweep items in one Weaviate batch and add them in order."""
                        batch_hashes = [it["content_hash"] for it in batch_items]
                        groups_map, metadata_map = _load_groups_and_metadata(batch_hashes)

                        for item in batch_items:
                            ch = item["content_hash"]
                            meta = metadata_map.get(ch, {})
                            source_file = item.get("source_file", meta.get("source_file", ""))
                            if _should_skip_source(source_file):
                                log.info(f"  Skipping {ch} — matches skip pattern: {source_file[:60]}")
                                continue

                            groups = groups_map.get(ch, [])
                            if not groups:
                                continue

                            if len(groups) > MAX_GROUPS_PER_HASH:
                                groups = groups[:MAX_GROUPS_PER_HASH]

                            _add_groups_to_pkg(item, groups, meta)
                            if len(pkg_items) >= max_items or actual_tokens >= content_budget:
                                return True
                        return False

                    # Pass 1: small/medium items (≤ MAX_GROUPS_PER_HASH groups)
                    pending_sweep = []
                    for item in sweep_items:
                        ch = item["content_hash"]
                        if ch in seen_hashes:
                            continue
                        seen_hashes.add(ch)
                        pending_sweep.append(item)
                        if len(pending_sweep) < WEAVIATE_TILE_BATCH_SIZE:
                            continue
                        if _process_sweep_batch(pending_sweep):
                            break
                        pending_sweep = []

                    if pending_sweep and len(pkg_items) < max_items and actual_tokens < content_budget:
                        _process_sweep_batch(pending_sweep)

                    unique_hashes = len(set(it["content_hash"] for it in pkg_items))
                    log.info(f"  Sweep added {added_sweep} items ({unique_hashes} unique hashes)")
                else:
                    log.info("Sweep found no new items — queue truly empty")
            break
        theme_id = theme_data["theme_id"]
        tried_themes.add(theme_id)

        if not primary_theme_data:
            primary_theme_data = theme_data

        theme_name = theme_data["display_name"]
        log.info(f"Adding from Theme {theme_id} ({theme_name}) — {actual_tokens:,}/{content_budget:,} tokens so far")

        candidates = _gather_candidates_from_theme(theme_data, seen_hashes)
        if not candidates:
            log.info(f"  No available items in theme {theme_id}, trying next")
            continue

        themes_used.append(f"{theme_id} ({theme_name})")
        added_this_theme = 0
        theme_max_items = PLATFORM_MAX_ITEMS.get(platform, MAX_ITEMS_PER_PKG)

        # Two-pass: small items first (hash diversity), large items fill remaining budget
        def _process_theme_candidate(item_type, item, groups, meta, score=0):
            nonlocal actual_tokens, added_this_theme
            ch = item["content_hash"]
            source_file = item.get("source_file", meta.get("source_file", ""))
            for group in groups:
                remaining = content_budget - actual_tokens
                if group["token_count"] > remaining and len(pkg_items) >= 1:
                    break
                pkg_items.append({
                    "type": item_type,
                    "content_hash": ch,
                    "group_id": group["group_id"],
                    "tile_range": group["tile_range"],
                    "source_file": source_file,
                    "platform": meta.get("platform", ""),
                    "session_id": meta.get("session_id", ""),
                    "exchange_index": meta.get("exchange_index"),
                    "content": group["content"],
                    "token_count": group["token_count"],
                    "score": score,
                })
                actual_tokens += group["token_count"]
                added_this_theme += 1
                if actual_tokens >= content_budget or len(pkg_items) >= theme_max_items:
                    break

        def _process_theme_batch(batch_candidates: list) -> bool:
            """Fetch theme candidates in one Weaviate batch and add them in order."""
            batch_hashes = [item["content_hash"] for _, item in batch_candidates]
            groups_map, metadata_map = _load_groups_and_metadata(batch_hashes)

            for item_type, item in batch_candidates:
                ch = item["content_hash"]
                meta = metadata_map.get(ch, {})
                source_file = item.get("source_file", meta.get("source_file", ""))
                if _should_skip_source(source_file):
                    log.info(f"  Skipping {ch} — matches skip pattern: {source_file[:60]}")
                    continue

                groups = groups_map.get(ch, [])
                if not groups:
                    continue

                if len(groups) > MAX_GROUPS_PER_HASH:
                    log.info(f"  {ch[:12]}: {len(groups)} groups, capping at {MAX_GROUPS_PER_HASH}")
                    groups = groups[:MAX_GROUPS_PER_HASH]

                _process_theme_candidate(item_type, item, groups, meta, item.get("score", 0))
                if actual_tokens >= content_budget or len(pkg_items) >= theme_max_items:
                    return True
            return False

        # Pass 1: small/medium items (≤ MAX_GROUPS_PER_HASH groups)
        pending_candidates = []
        for item_type, item in candidates:
            ch = item["content_hash"]
            if ch in seen_hashes:
                continue
            seen_hashes.add(ch)
            pending_candidates.append((item_type, item))
            if len(pending_candidates) < WEAVIATE_TILE_BATCH_SIZE:
                continue
            if _process_theme_batch(pending_candidates):
                break
            pending_candidates = []

        if pending_candidates and actual_tokens < content_budget and len(pkg_items) < theme_max_items:
            _process_theme_batch(pending_candidates)

        log.info(f"  Added {added_this_theme} items from theme {theme_id}")

        if actual_tokens >= content_budget:
            break

    if not pkg_items:
        log.error("No content retrieved — package empty")
        return ""

    # Use primary theme for anchors and naming
    theme_data = primary_theme_data
    theme_id = theme_data["theme_id"]
    theme_name = theme_data["display_name"]

    # Get anchors from primary theme
    anchors = theme_data.get("anchor_rosettas", [])[:MAX_ANCHORS]

    # Generate package ID (multi-theme gets "multi" suffix)
    if len(themes_used) > 1:
        pkg_id = f"pkg_{theme_id}_multi_{platform}_{int(time.time())}"
    else:
        pkg_id = f"pkg_{theme_id}_{platform}_{int(time.time())}"

    # Write markdown
    os.makedirs(PKG_DIR, exist_ok=True)
    pkg_path = os.path.join(PKG_DIR, f"{pkg_id}.md")

    # Deduplicate content_hashes (tile groups share the same base hash)
    content_hashes = list(dict.fromkeys(it["content_hash"] for it in pkg_items))
    _write_package_markdown(pkg_path, pkg_id, theme_data, anchors, pkg_items, platform)

    # Mark items in-progress (by base content_hash)
    mark_in_progress(content_hashes, platform, pkg_id)

    # Store current package info
    set_current_package(platform, {
        "pkg_id": pkg_id,
        "theme_id": theme_id,
        "theme_name": theme_name,
        "themes": themes_used,
        "platform": platform,
        "content_hashes": content_hashes,
        "item_count": len(pkg_items),
        "total_tokens": actual_tokens,
        "pkg_path": pkg_path,
        "created_at": time.time(),
    })

    log.info(f"Package built: {pkg_path}")
    log.info(f"  Items: {len(pkg_items)}, Tokens: {actual_tokens:,}, "
             f"Themes: {len(themes_used)}, Anchors: {len(anchors)}")

    return pkg_path


def _write_package_markdown(path: str, pkg_id: str, theme: dict, anchors: list,
                            items: list, platform: str):
    """Write the package as a markdown file."""
    lines = []

    # Instructions (included in file so worker just attaches + sends one-liner)
    # Use template with actual item count for better AI compliance
    lines.append("# INSTRUCTIONS")
    lines.append("")
    lines.append(ANALYSIS_PROMPT_TEMPLATE.format(item_count=len(items)))
    lines.append("")
    lines.append("---")
    lines.append("")

    # Header
    lines.append(f"# Theme: {theme['display_name']} — Analysis Package")
    lines.append(f"**Package ID**: {pkg_id}")
    lines.append(f"**Theme**: {theme['theme_id']} — {theme['description']}")
    lines.append(f"**Required Motifs**: {', '.join(theme.get('required_motifs', []))}")
    lines.append(f"**Supporting Motifs**: {', '.join(theme.get('supporting_motifs', []))}")
    lines.append(f"**Items**: {len(items)} | **Platform**: {platform}")
    lines.append("")

    # Context Anchors
    if anchors:
        lines.append("---")
        lines.append("")
        lines.append("## Context Anchors (already compressed — use as quality reference)")
        lines.append("")
        for i, anchor in enumerate(anchors, 1):
            src = anchor.get("source_file", "unknown")
            # Shorten source path
            short_src = src.split("/corpus/")[-1] if "/corpus/" in src else src.split("/")[-1]
            lines.append(f"### ANCHOR-{i} [{short_src}]")
            rosetta = anchor.get("rosetta_summary", "")
            if rosetta:
                lines.append(f"**Rosetta**: {rosetta}")
            motifs = anchor.get("dominant_motifs", [])
            if motifs:
                lines.append(f"**Motifs**: {', '.join(motifs)}")
            lines.append("")

    # Items for Analysis
    lines.append("---")
    lines.append("")
    lines.append("## Items for Analysis")
    lines.append("")

    for i, item in enumerate(items, 1):
        # Use group_id (includes @tile_range) if present, else content_hash
        item_hash = item.get("group_id", item["content_hash"])
        src = item.get("source_file", "unknown")
        short_src = src.split("/corpus/")[-1] if "/corpus/" in src else (
            src.split("/parsed/")[-1] if "/parsed/" in src else src.split("/")[-1])

        tile_info = ""
        if item.get("tile_range"):
            s, e = item["tile_range"]
            tile_info = f" (tiles {s}-{e})"

        if item["type"] == "CORPUS":
            lines.append(f"### ITEM-{i:03d} [CORPUS] {short_src}{tile_info} [{item_hash}]")
        else:
            plat = item.get("platform", "")
            sess = item.get("session_id", "")[:12]
            eidx = item.get("exchange_index", "")
            lines.append(f"### ITEM-{i:03d} [TRANSCRIPT] {plat}/{short_src}{tile_info} [{item_hash}]")
            if sess:
                lines.append(f"*Session: {sess}, Exchange: {eidx}*")

        lines.append("")
        lines.append(item["content"])
        lines.append("")
        lines.append("---")
        lines.append("")

    # Write file
    with open(path, "w") as f:
        f.write("\n".join(lines))


# ============================================================================
# Analysis Prompt
# ============================================================================

ANALYSIS_PROMPT_TEMPLATE = """Analyze all {item_count} items in this package. You MUST produce exactly {item_count} items in your response.

For each item provide:
1. Rosetta summary (2-4 dense sentences, self-contained, precise)
2. Motif assignments with amplitude (0-1) and confidence (0-1). Available motifs:
""" + "\n".join(f"- {mid}: {V0_MOTIFS[mid].definition[:100]}" for mid in sorted(V0_MOTIFS.keys())) + """
3. Cross-references between items (extends|contradicts|references|builds_on)

CRITICAL: Echo back the FULL content hash shown in [brackets] for each item — all 16 characters, not truncated.

Respond ONLY with MINIFIED JSON on a single line (no newlines, no indentation, no markdown, no explanation). Escape all quotes inside string values. Output must be valid JSON parseable by json.loads():
{{"package_id":"...","package_summary":"...","items":[{{"hash":"<FULL 16-char content hash>","rosetta_summary":"...","motifs":[{{"motif_id":"HMM.X","amp":0.85,"confidence":0.9}}],"cross_refs":[{{"target":"<hash>","type":"extends","note":"..."}}]}}]}}

VERIFY before responding: Your items array MUST contain exactly {item_count} objects — one per ITEM in the package."""

# Legacy prompt for get_prompt() (no item count — used by workers who call `prompt` command)
ANALYSIS_PROMPT = ANALYSIS_PROMPT_TEMPLATE.format(item_count="ALL")


def get_prompt() -> str:
    """Return the analysis prompt to send to AI platforms."""
    return ANALYSIS_PROMPT


# ============================================================================
# Completion & Failure
# ============================================================================

def complete_package(platform: str = None):
    """Mark the current package as completed."""
    pkg = None
    if platform:
        pkg = get_current_package(platform)
    else:
        for p in PLATFORM_BUDGETS:
            pkg = get_current_package(p)
            if pkg:
                platform = p
                break

    if not pkg:
        log.error("No current package found to complete")
        return False

    hashes = pkg.get("content_hashes", [])
    pkg_id = pkg.get("pkg_id", "")
    verified = mark_completed(hashes, pkg_id=pkg_id)
    clear_current_package(platform)

    # Update stats — only count verified items (prevents counter inflation)
    r = get_redis()
    r.hincrby(f"{PFX}stats", "completed_packages", 1)
    r.hincrby(f"{PFX}stats", "completed_items", verified)

    if verified < len(hashes):
        log.warning(f"Completed package {pkg_id} — {verified}/{len(hashes)} items verified (ownership mismatch on {len(hashes) - verified})")
    else:
        log.info(f"Completed package {pkg_id} — {verified} items marked done")
    return True


def fail_package(reason: str, platform: str = None):
    """Mark the current package as failed and requeue items."""
    pkg = None
    if platform:
        pkg = get_current_package(platform)
    else:
        for p in PLATFORM_BUDGETS:
            pkg = get_current_package(p)
            if pkg:
                platform = p
                break

    if not pkg:
        log.error("No current package found to fail")
        return False

    hashes = pkg.get("content_hashes", [])
    # Remove in-progress markers (items become available again)
    pipe = get_redis().pipeline()
    for ch in hashes:
        pipe.delete(f"{PFX}in_progress:{ch}")
    pipe.execute()
    clear_current_package(platform)

    # Update stats
    r = get_redis()
    r.hincrby(f"{PFX}stats", "failed_packages", 1)

    log.info(f"Failed package {pkg['pkg_id']} — reason: {reason}")
    log.info(f"  {len(hashes)} items requeued")
    return True


def re_enrich_batch(count: int = 500):
    """Move a batch of completed items back to available for re-enrichment.

    Used when motif dictionary is expanded (v0.1.0 -> v0.2.0) so tiles
    get re-analyzed with the new motifs. Items removed from completed set
    become available for `next` to pick up.

    Args:
        count: Number of items to make available.

    Returns:
        Number of items moved.
    """
    r = get_redis()
    completed_key = f"{PFX}completed"

    # Pop random members from the completed set
    moved = 0
    # Use SRANDMEMBER + SREM (SPOP not available in all versions)
    members = r.srandmember(completed_key, count)
    if members:
        r.srem(completed_key, *members)
        moved = len(members)

    log.info(f"Re-enrich: moved {moved} items from completed back to available")
    return moved


# ============================================================================
# Stats
# ============================================================================

def _count_weaviate_unenriched() -> int:
    """Count tiles in Weaviate where hmm_enriched != true and scale=search_512."""
    try:
        q = f"""{{
            Aggregate {{
                {WEAVIATE_CLASS}(where: {{
                    operator: And,
                    operands: [
                        {{ path: ["hmm_enriched"], operator: NotEqual, valueBoolean: true }},
                        {{ path: ["scale"], operator: Equal, valueText: "search_512" }}
                    ]
                }}) {{
                    meta {{ count }}
                }}
            }}
        }}"""
        data = weaviate_gql(q, timeout=30)
        agg = data.get("Aggregate", {}).get(WEAVIATE_CLASS, [{}])
        return agg[0].get("meta", {}).get("count", -1) if agg else -1
    except Exception as e:
        log.warning(f"Could not count unenriched tiles: {e}")
        return -1


def show_stats():
    """Show overall progress statistics."""
    r = get_redis()
    index = load_index()

    completed = r.scard(f"{PFX}completed")
    stats = r.hgetall(f"{PFX}stats")

    # Count total unique items in index
    all_corpus = set()
    all_exchanges = set()
    for theme in index.values():
        for it in theme.get("unenriched_corpus", []):
            all_corpus.add(it["content_hash"])
        for it in theme.get("unenriched_exchanges", []):
            all_exchanges.add(it["content_hash"])

    total = len(all_corpus) + len(all_exchanges)
    in_progress = 0
    for key in r.scan_iter(f"{PFX}in_progress:*"):
        in_progress += 1

    # Current packages
    current = {}
    for p in PLATFORM_BUDGETS:
        pkg = get_current_package(p)
        if pkg:
            current[p] = pkg

    # Real unenriched count from Weaviate (ground truth)
    weaviate_unenriched = _count_weaviate_unenriched()

    theme_remaining = total - completed - in_progress

    print(f"\n{'='*60}")
    print(f"HMM Package Builder — Progress")
    print(f"{'='*60}")
    print(f"  Theme-indexed items:   {total:>8,}")
    print(f"    Corpus:              {len(all_corpus):>8,}")
    print(f"    Exchanges:           {len(all_exchanges):>8,}")
    print(f"  Completed hashes:      {completed:>8,}")
    print(f"  In progress:           {in_progress:>8,}")
    if theme_remaining > 0:
        print(f"  Theme queue remaining: {theme_remaining:>8,}")
    else:
        print(f"  Theme queue:           EXHAUSTED (sweep mode active)")
    if weaviate_unenriched >= 0:
        print(f"  Weaviate unenriched:   {weaviate_unenriched:>8,}  ← GROUND TRUTH")
    else:
        print(f"  Weaviate unenriched:   (query failed)")
    print(f"  Completed packages:    {stats.get('completed_packages', 0):>8}")
    print(f"  Failed packages:       {stats.get('failed_packages', 0):>8}")

    if current:
        print(f"\n  Active packages:")
        for p, pkg in current.items():
            age = time.time() - pkg.get("created_at", 0)
            print(f"    {p:12s}: {pkg['pkg_id']} ({pkg['item_count']} items, "
                  f"{pkg['total_tokens']:,} tokens, {age/60:.0f}m ago)")
    else:
        print(f"\n  No active packages")

    if weaviate_unenriched and weaviate_unenriched > 0:
        print(f"\n  *** {weaviate_unenriched:,} tiles still need enrichment — DO NOT STOP ***")

    print()


def reset_state():
    """Clear all package builder state from Redis."""
    r = get_redis()
    keys = list(r.scan_iter(f"{PFX}*"))
    if keys:
        r.delete(*keys)
        print(f"Cleared {len(keys)} Redis keys")
    else:
        print("No state to clear")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="HMM Package Builder")
    sub = parser.add_subparsers(dest="command")

    next_cmd = sub.add_parser("next", help="Build next package")
    next_cmd.add_argument("--platform", required=True, choices=list(PLATFORM_BUDGETS.keys()))

    complete_cmd = sub.add_parser("complete", help="Mark current package done")
    complete_cmd.add_argument("--platform", choices=list(PLATFORM_BUDGETS.keys()))
    complete_cmd.add_argument("--response-file", required=True, help="Path to AI response file — REQUIRED, processes via hmm_store_results")

    fail_cmd = sub.add_parser("fail", help="Mark current package failed")
    fail_cmd.add_argument("reason", nargs="?", default="unknown")
    fail_cmd.add_argument("--platform", choices=list(PLATFORM_BUDGETS.keys()))

    sub.add_parser("stats", help="Show progress")
    sub.add_parser("reset", help="Clear all state")
    sub.add_parser("prompt", help="Print analysis prompt")

    reenrich_cmd = sub.add_parser("re-enrich", help="Move completed items back for re-enrichment with expanded motifs")
    reenrich_cmd.add_argument("--count", type=int, default=500, help="Number of items to make available (default 500)")

    args = parser.parse_args()

    if args.command == "next":
        path = build_package(args.platform)
        if path:
            print(f"\nPackage ready: {path}")
            print(f"\nAnalysis prompt:")
            print(ANALYSIS_PROMPT)
        else:
            print("No package built — check logs above")
            sys.exit(1)

    elif args.command == "complete":
        platform = getattr(args, "platform", None)
        response_file = getattr(args, "response_file", None)

        # 6SIGMA: --response-file is MANDATORY. Never mark items complete without storing data.
        if not response_file:
            log.error("HALT: --response-file is required. Cannot mark items complete without storing response data.")
            log.error("Usage: complete --platform <name> --response-file <path>")
            sys.exit(1)

        if not os.path.exists(response_file):
            log.error(f"HALT: Response file not found: {response_file}")
            sys.exit(1)

        # Pre-validate: response must contain JSON with "items" array.
        # Catches prompt text extraction (worker copied user message instead of AI response).
        with open(response_file) as _rf:
            raw_response = _rf.read().strip()
        if not raw_response:
            log.error("HALT: Response file is empty")
            fail_package("empty_response_file", platform)
            sys.exit(1)
        # Quick check: response should contain JSON markers
        if '"items"' not in raw_response and '"package_id"' not in raw_response:
            snippet = raw_response[:200].replace('\n', ' ')
            log.error(f"HALT: Response does not look like valid HMM JSON: {snippet}")
            log.error("Likely extracted prompt text instead of AI response. Items requeued.")
            fail_package("not_json_response", platform)
            sys.exit(1)

        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from hmm_store_results import process_response

        pkg = get_current_package(platform) if platform else None
        if not pkg:
            log.error("No current package found to complete")
            sys.exit(1)
        pkg_id = pkg.get("pkg_id", "")
        pkg_hashes = set(pkg.get("content_hashes", []))

        result = process_response(
            response_file,
            platform=platform or "unknown",
            pkg_id=pkg_id,
            pkg_hashes=pkg_hashes,
        )

        if not result.get("success"):
            stored = result.get('stored', 0)
            parsed = result.get('parsed', 0)
            failed = result.get('failed', 0)
            log.error(f"STORAGE FAILED: {stored}/{parsed} stored, {failed} failed")
            log.error(f"Response file preserved: {response_file}")
            # Requeue ALL items — do NOT mark any complete
            fail_package(f"storage_failed: {stored}/{parsed} stored", platform)
            log.error(f"Items requeued. Fix issue and rebuild package.")
            sys.exit(1)

        # 6SIGMA: Only mark STORED hashes as complete, not the full package.
        # If AI returned 7 items from a 90-item package, only those 7 get marked done.
        stored_hashes = set(result.get("stored_hashes", []))
        unstored_hashes = pkg_hashes - stored_hashes

        if unstored_hashes:
            log.warning(f"PARTIAL RESPONSE: {len(stored_hashes)}/{len(pkg_hashes)} items stored")
            log.warning(f"  {len(unstored_hashes)} items NOT in response — requeuing them")
            # Requeue unstored items (delete in-progress keys so they become available)
            r = get_redis()
            pipe = r.pipeline()
            for ch in unstored_hashes:
                pipe.delete(f"{PFX}in_progress:{ch}")
            pipe.execute()

        # Mark only stored hashes as completed
        if stored_hashes:
            verified = mark_completed(list(stored_hashes), pkg_id=pkg_id)
            r = get_redis()
            r.hincrby(f"{PFX}stats", "completed_packages", 1)
            r.hincrby(f"{PFX}stats", "completed_items", verified)
            log.info(f"Completed: {verified} items marked done (of {len(pkg_hashes)} in package)")
        else:
            log.error("Zero items stored — failing package")
            fail_package("zero_items_stored", platform)
            sys.exit(1)

        clear_current_package(platform)

    elif args.command == "fail":
        platform = getattr(args, "platform", None)
        if not fail_package(args.reason, platform):
            sys.exit(1)

    elif args.command == "stats":
        show_stats()

    elif args.command == "reset":
        reset_state()

    elif args.command == "re-enrich":
        moved = re_enrich_batch(args.count)
        print(f"Moved {moved} items from completed back to available for re-enrichment")
        print(f"Run `next --platform <name>` to build packages with expanded v0.2.0 motif dictionary")

    elif args.command == "prompt":
        print(ANALYSIS_PROMPT)

    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\nInterrupted")
    except Exception:
        log.error("Fatal error:", exc_info=True)
        sys.exit(1)
