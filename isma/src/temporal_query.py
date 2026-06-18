"""
ISMA Temporal Query — Time-aware filtering and scoring for retrieval.

Provides:
  - Temporal decay scoring (exponential decay with configurable half-life)
  - Time-bounded filtering via loaded_at property
  - Recency boosting for search results

Half-lives (configurable):
  - Conceptual content: 180 days (slow decay — concepts stay relevant)
  - Operational content: 30 days (fast decay — ops info goes stale)
  - Default: 90 days

Usage:
    from isma.src.temporal_query import apply_temporal_decay, build_time_filter

    # Decay scoring
    tiles = apply_temporal_decay(tiles, half_life_days=90)

    # Time filtering
    filter_clause = build_time_filter(after="2026-01-01", before="2026-02-01")
"""

import math
import re
import time
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Half-lives by query type (days)
HALF_LIVES = {
    "exact": 90,
    "temporal": 60,
    "conceptual": 180,
    "relational": 120,
    "memory": 120,
    "humor": 120,
    "motif": 120,
    "default": 90,
}


def parse_loaded_at(loaded_at: str) -> Optional[float]:
    """Parse loaded_at string to Unix timestamp.

    Handles formats:
      - ISO 8601: 2026-01-15T12:00:00Z
      - Date only: 2026-01-15
      - Datetime: 2026-01-15 12:00:00
    """
    if not loaded_at:
        return None

    for fmt in [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(loaded_at, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def temporal_decay_score(
    loaded_at: str,
    half_life_days: float = 90,
    reference_time: Optional[float] = None,
) -> float:
    """Compute temporal decay score for a tile based on its age.

    Uses exponential decay: score = 2^(-age/half_life)

    Returns:
        Float in (0, 1]. 1.0 = just loaded, 0.5 = one half-life ago.
        Returns 1.0 if loaded_at can't be parsed (benefit of the doubt).
    """
    ts = parse_loaded_at(loaded_at)
    if ts is None:
        return 1.0

    now = reference_time or time.time()
    age_days = (now - ts) / 86400

    if age_days <= 0:
        return 1.0

    return math.pow(2, -age_days / half_life_days)


def apply_temporal_decay(
    tiles: list,
    half_life_days: float = 90,
    decay_weight: float = 0.2,
) -> list:
    """Apply temporal decay to tile scores.

    New score = (1 - decay_weight) * original_score + decay_weight * temporal_score

    Args:
        tiles: List of TileResult objects (must have .score and loaded_at or source_file)
        half_life_days: Half-life for decay
        decay_weight: How much temporal decay affects final score (0-1)

    Returns:
        Tiles sorted by adjusted score (descending).
    """
    now = time.time()
    scored_tiles = []

    for tile in tiles:
        # Get loaded_at from tile
        loaded_at = ""
        if hasattr(tile, "loaded_at"):
            loaded_at = tile.loaded_at or ""

        # Fall back to extracting date from source_file
        if not loaded_at and hasattr(tile, "source_file"):
            loaded_at = _extract_date_from_path(tile.source_file or "")

        temporal_score = temporal_decay_score(loaded_at, half_life_days, now)
        original_score = getattr(tile, "score", 0.0) or 0.0
        if isinstance(original_score, str):
            try:
                original_score = float(original_score)
            except (ValueError, TypeError):
                original_score = 0.0

        adjusted = (1 - decay_weight) * original_score + decay_weight * temporal_score
        # Create copy with updated score (never mutate in-place — may be cached)
        try:
            tile = dc_replace(tile, score=adjusted)
        except TypeError:
            # Fallback for non-dataclass objects
            if hasattr(tile, "_replace"):
                tile = tile._replace(score=adjusted)
            else:
                tile.score = adjusted

        scored_tiles.append(tile)

    scored_tiles.sort(key=lambda t: t.score, reverse=True)
    return scored_tiles


def build_time_filter_gql(
    after: Optional[str] = None,
    before: Optional[str] = None,
) -> str:
    """Build a Weaviate where clause for time-bounded queries.

    Uses loaded_at property (text field with ISO format).

    Args:
        after: ISO date string (inclusive) e.g., "2026-01-01"
        before: ISO date string (exclusive) e.g., "2026-02-01"

    Returns:
        GraphQL where clause fragment (empty string if no bounds).
    """
    conditions = []

    if after:
        conditions.append(
            f'{{ path: ["loaded_at"], operator: GreaterThanEqual, valueText: "{after}" }}'
        )
    if before:
        conditions.append(
            f'{{ path: ["loaded_at"], operator: LessThan, valueText: "{before}" }}'
        )

    if not conditions:
        return ""

    if len(conditions) == 1:
        return conditions[0]

    return f'{{ operator: And, operands: [{", ".join(conditions)}] }}'


def recency_sort(tiles: list) -> list:
    """Sort tiles by loaded_at descending (most recent first).

    Tiles without loaded_at go to the end.
    """
    def sort_key(tile):
        loaded_at = ""
        if hasattr(tile, "loaded_at"):
            loaded_at = tile.loaded_at or ""
        if not loaded_at and hasattr(tile, "source_file"):
            loaded_at = _extract_date_from_path(tile.source_file or "")
        ts = parse_loaded_at(loaded_at)
        return ts or 0.0

    return sorted(tiles, key=sort_key, reverse=True)


def _extract_date_from_path(path: str) -> str:
    """Try to extract a date from a file path.

    Common patterns:
      - /conversations/2026-01-15/session.md
      - /corpus/layer_2/claude/2025-12-17_example.md
      - /data_20260115.json
    """
    # YYYY-MM-DD in path
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path)
    if match:
        return match.group(1)

    # YYYYMMDD in path
    match = re.search(r"(\d{4})(\d{2})(\d{2})", path)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    return ""
