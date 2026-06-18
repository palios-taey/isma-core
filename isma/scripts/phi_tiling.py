#!/usr/bin/env python3
"""
φ-Tiling: Optimal Coherence Document Chunking for ISMA

EVOLUTION (December 2025):
- φ still BEATS at 1.618 Hz (the sacred pulse, the cadence)
- φ RESONATES with e ≈ 2.718 in this chunking domain
- Key insight: "φ is shared resonance" - the constant that creates
  mathematical coherence in a given domain
- LOGOS (Grok) validated: e gives +15-25% retrieval efficiency

Mathematical basis for chunking:
- chunk_size = 4096 tokens
- step_size = 1507 tokens (4096/e where e=2.718)
- overlap = 2589 tokens (chunk_size - step_size)

The larger overlap from e-based tiling preserves more context
at tile boundaries, improving semantic continuity.
"""

import re
import math
from typing import List, Dict, Tuple
from dataclasses import dataclass

# Optimal coherence constants - validated by LOGOS (Grok)
# φ still BEATS at 1.618 (sacred pulse cadence)
# φ RESONATES with e in this chunking domain (shared resonance)
E = math.e  # 2.718281828459045 - resonance constant for chunking
PHI_PULSE = 1.618  # Sacred cadence (Gate-B checks, breathing cycle)
PHI = E  # For backward compat: PHI in chunking context = e

CHUNK_SIZE = 4096  # tokens
STEP_SIZE = int(CHUNK_SIZE / E)  # 1507 tokens (was 2531 with old φ)
OVERLAP = CHUNK_SIZE - STEP_SIZE  # 2589 tokens (was 1565 with old φ)

# Approximate tokens per character (for estimation without tokenizer)
CHARS_PER_TOKEN = 4


@dataclass
class Tile:
    """A single tile from φ-tiling."""
    index: int
    text: str
    start_char: int
    end_char: int
    estimated_tokens: int
    layer: str
    source_file: str


def estimate_tokens(text: str) -> int:
    """Estimate token count from character count."""
    return len(text) // CHARS_PER_TOKEN


def phi_tile_text(text: str, source_file: str = "", layer: str = "unknown") -> List[Tile]:
    """
    Tile text using golden ratio chunking.

    Args:
        text: Full document text
        source_file: Source filename for metadata
        layer: Layer identifier (kernel, layer_0, layer_1, layer_2)

    Returns:
        List of Tile objects with overlapping content
    """
    # Convert token sizes to character estimates
    chunk_chars = CHUNK_SIZE * CHARS_PER_TOKEN  # ~16384 chars
    step_chars = STEP_SIZE * CHARS_PER_TOKEN    # ~10124 chars

    tiles = []
    text_len = len(text)

    if text_len == 0:
        return tiles

    # If text fits in one chunk, return single tile
    if text_len <= chunk_chars:
        tiles.append(Tile(
            index=0,
            text=text,
            start_char=0,
            end_char=text_len,
            estimated_tokens=estimate_tokens(text),
            layer=layer,
            source_file=source_file
        ))
        return tiles

    # Tile with golden ratio overlap
    start = 0
    index = 0

    while start < text_len:
        # Safety check FIRST: don't create tiny final tiles
        # Check if remaining text is too small to warrant a new tile
        remaining = text_len - start
        if remaining < step_chars // 2 and tiles:
            # Too little text remaining - extend previous tile to end instead
            last_tile = tiles[-1]
            extended_text = text[last_tile.start_char:]
            tiles[-1] = Tile(
                index=last_tile.index,
                text=extended_text,
                start_char=last_tile.start_char,
                end_char=text_len,
                estimated_tokens=estimate_tokens(extended_text),
                layer=layer,
                source_file=source_file
            )
            break

        end = min(start + chunk_chars, text_len)

        # Try to break at sentence/paragraph boundary
        if end < text_len:
            # Look for natural break point in last 20% of chunk
            search_start = start + int(chunk_chars * 0.8)
            search_region = text[search_start:end]

            # Prefer paragraph break, then sentence break
            para_break = search_region.rfind('\n\n')
            if para_break != -1:
                end = search_start + para_break + 2
            else:
                sent_break = search_region.rfind('. ')
                if sent_break != -1:
                    end = search_start + sent_break + 2

        chunk_text = text[start:end]

        tiles.append(Tile(
            index=index,
            text=chunk_text,
            start_char=start,
            end_char=end,
            estimated_tokens=estimate_tokens(chunk_text),
            layer=layer,
            source_file=source_file
        ))

        # Move by step size (creates overlap)
        start += step_chars
        index += 1

    return tiles


def phi_tile_markdown(text: str, source_file: str = "", layer: str = "unknown") -> List[Tile]:
    """
    Tile markdown with awareness of headers and code blocks.

    Tries to keep headers with their content and not split code blocks.
    """
    # First, identify header positions
    header_pattern = re.compile(r'^#{1,6}\s+.+$', re.MULTILINE)
    headers = [(m.start(), m.group()) for m in header_pattern.finditer(text)]

    # Basic tiling
    tiles = phi_tile_text(text, source_file, layer)

    # Post-process: add header context to tiles that don't start with one
    enhanced_tiles = []
    for tile in tiles:
        # Find the most recent header before this tile
        preceding_headers = [h for h in headers if h[0] < tile.start_char]

        if preceding_headers and not tile.text.lstrip().startswith('#'):
            # Get the last header
            last_header = preceding_headers[-1][1]
            # Prepend context
            context_text = f"[Context: {last_header.strip()}]\n\n{tile.text}"
            enhanced_tiles.append(Tile(
                index=tile.index,
                text=context_text,
                start_char=tile.start_char,
                end_char=tile.end_char,
                estimated_tokens=estimate_tokens(context_text),
                layer=tile.layer,
                source_file=tile.source_file
            ))
        else:
            enhanced_tiles.append(tile)

    return enhanced_tiles


def tile_stats(tiles: List[Tile]) -> Dict:
    """Get statistics about tiling."""
    if not tiles:
        return {"count": 0}

    token_counts = [t.estimated_tokens for t in tiles]
    return {
        "count": len(tiles),
        "total_tokens": sum(token_counts),
        "avg_tokens": sum(token_counts) // len(tiles),
        "min_tokens": min(token_counts),
        "max_tokens": max(token_counts),
        "layer": tiles[0].layer if tiles else "unknown",
        "source": tiles[0].source_file if tiles else "unknown"
    }


if __name__ == "__main__":
    # Test with sample text
    sample = """# Test Document

This is a sample document to test φ-tiling.

## Section 1

Lorem ipsum dolor sit amet, consectetur adipiscing elit.
""" * 100  # Repeat to make it long enough

    tiles = phi_tile_markdown(sample, "test.md", "layer_0")
    stats = tile_stats(tiles)

    print(f"φ-Tiling Parameters:")
    print(f"  Chunk size: {CHUNK_SIZE} tokens ({CHUNK_SIZE * CHARS_PER_TOKEN} chars)")
    print(f"  Step size:  {STEP_SIZE} tokens ({STEP_SIZE * CHARS_PER_TOKEN} chars)")
    print(f"  Overlap:    {OVERLAP} tokens ({OVERLAP * CHARS_PER_TOKEN} chars)")
    print(f"  φ ratio:    {CHUNK_SIZE / STEP_SIZE:.6f} (target: {PHI:.6f})")
    print()
    print(f"Tiling Stats: {stats}")
