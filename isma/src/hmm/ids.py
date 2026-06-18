"""
HMM ID generation and text canonicalization.

All IDs are content-addressed via SHA-256 for exact dedup and rebuildability.
This is "cannot lie" at the substrate layer.
"""

import hashlib
import re


def canonicalize_text(text: str) -> str:
    """
    Canonicalize text for deterministic hashing.

    Steps:
    1. Normalize line endings to \\n
    2. Strip trailing whitespace per line
    3. Collapse >3 blank lines to 2
    4. UTF-8 decode errors replaced deterministically
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")

    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Strip trailing whitespace per line
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)

    # Collapse >3 blank lines to 2
    text = re.sub(r"\n{4,}", "\n\n\n", text)

    return text


def artifact_id(file_bytes: bytes) -> str:
    """Content-addressed artifact ID from raw file bytes."""
    return hashlib.sha256(file_bytes).hexdigest()


def tile_id(tile_text: str) -> str:
    """Content-addressed tile ID from canonicalized tile text."""
    canonical = canonicalize_text(tile_text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(text: str, prefix_len: int = 16) -> str:
    """Short content hash for dedup keys."""
    canonical = canonicalize_text(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:prefix_len]
