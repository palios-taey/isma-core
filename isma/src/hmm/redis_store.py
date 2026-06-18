"""
HMM Redis Store - functional workspace layer.

Provides:
  - Inverted motif index (motif_id -> set of tile_ids)
  - Resonance fields (multi-timescale amplitude vectors over motifs)
  - Tile motif cache (optional fast lookup)
  - Working set per session

Phase 5 note: Inverted index READS (inv_get, inv_union, inv_intersect)
are deprecated in favor of v2 adaptive search. Writes continue for
backward compatibility. Read methods log deprecation warnings.
"""

import json
import logging
import math
import time
import warnings
from typing import List, Dict, Optional, Set
from dataclasses import dataclass

import redis
from isma.config import REDIS_HOST as CONFIG_REDIS_HOST, REDIS_PORT as CONFIG_REDIS_PORT

from .motifs import MotifAssignment

log = logging.getLogger(__name__)

# Redis connection (shared ISMA instance)
REDIS_HOST = CONFIG_REDIS_HOST
REDIS_PORT = CONFIG_REDIS_PORT

# Key prefix - all HMM keys under hmm:
PREFIX = "hmm:"

# Resonance field time constants (tau_k = tau_0 * phi^k)
# phi = e (2.718) per the evolved constant
PHI = math.e
TAU_0 = 60.0  # base half-life in seconds for fast field


@dataclass
class ResonanceField:
    """A resonance field state at a given timescale k."""
    k: int
    amplitudes: Dict[str, float]  # motif_id -> amplitude
    last_updated: float = 0.0

    @property
    def tau(self) -> float:
        """Time constant for this field's decay."""
        return TAU_0 * (PHI ** self.k)


class HMMRedisStore:
    """Redis store for HMM live workspace data."""

    def __init__(self, host: str = REDIS_HOST, port: int = REDIS_PORT):
        self.r = redis.Redis(host=host, port=port, decode_responses=True)

    # --- Inverted Index ---

    def inv_add(self, motif_id: str, tile_id: str):
        """Add tile to motif's inverted index."""
        self.r.sadd(f"{PREFIX}inv:{motif_id}", tile_id)

    def inv_add_batch(self, motif_id: str, tile_ids: List[str]):
        """Add multiple tiles to motif's inverted index."""
        if tile_ids:
            self.r.sadd(f"{PREFIX}inv:{motif_id}", *tile_ids)

    def inv_get(self, motif_id: str) -> Set[str]:
        """Get all tile_ids for a motif.

        DEPRECATED: Use ISMARetrievalV2.adaptive_search() instead.
        """
        warnings.warn(
            "inv_get is deprecated — use ISMARetrievalV2.adaptive_search()",
            DeprecationWarning, stacklevel=2,
        )
        return self.r.smembers(f"{PREFIX}inv:{motif_id}")

    def inv_union(self, motif_ids: List[str]) -> Set[str]:
        """Union of tile sets for multiple motifs.

        DEPRECATED: Use ISMARetrievalV2.adaptive_search() instead.
        """
        warnings.warn(
            "inv_union is deprecated — use ISMARetrievalV2.adaptive_search()",
            DeprecationWarning, stacklevel=2,
        )
        if not motif_ids:
            return set()
        keys = [f"{PREFIX}inv:{mid}" for mid in motif_ids]
        return self.r.sunion(*keys)

    def inv_intersect(self, motif_ids: List[str]) -> Set[str]:
        """Intersection of tile sets for multiple motifs.

        DEPRECATED: Use ISMARetrievalV2.adaptive_search() instead.
        """
        warnings.warn(
            "inv_intersect is deprecated — use ISMARetrievalV2.adaptive_search()",
            DeprecationWarning, stacklevel=2,
        )
        if not motif_ids:
            return set()
        keys = [f"{PREFIX}inv:{mid}" for mid in motif_ids]
        return self.r.sinter(*keys)

    def inv_count(self, motif_id: str) -> int:
        """Count tiles for a motif."""
        return self.r.scard(f"{PREFIX}inv:{motif_id}")

    # --- Resonance Fields ---

    def field_update(self, k: int, motif_id: str, amp: float):
        """
        Update resonance field k for a motif.

        Currently uses max-amplitude strategy (before decay is implemented).
        """
        key = f"{PREFIX}field:{k}"
        current = self.r.hget(key, motif_id)
        if current is None or float(current) < amp:
            self.r.hset(key, motif_id, str(amp))

    def field_update_batch(self, k: int, assignments: List[MotifAssignment]):
        """Update resonance field k for multiple motifs at once."""
        if not assignments:
            return
        key = f"{PREFIX}field:{k}"
        pipe = self.r.pipeline()
        for a in assignments:
            pipe.hget(key, a.motif_id)
        current_vals = pipe.execute()

        pipe = self.r.pipeline()
        for a, current in zip(assignments, current_vals):
            if current is None or float(current) < a.amp:
                pipe.hset(key, a.motif_id, str(a.amp))
        pipe.execute()

    def field_get(self, k: int) -> Dict[str, float]:
        """Get all amplitudes in resonance field k."""
        key = f"{PREFIX}field:{k}"
        raw = self.r.hgetall(key)
        return {mid: float(amp) for mid, amp in raw.items()}

    def field_get_motif(self, k: int, motif_id: str) -> float:
        """Get amplitude for a specific motif in field k."""
        val = self.r.hget(f"{PREFIX}field:{k}", motif_id)
        return float(val) if val else 0.0

    def field_decay(self, k: int, elapsed_seconds: float):
        """
        Apply exponential decay to resonance field k.

        amp_new = amp * exp(-elapsed / tau_k)
        """
        tau_k = TAU_0 * (PHI ** k)
        decay_factor = math.exp(-elapsed_seconds / tau_k)

        key = f"{PREFIX}field:{k}"
        raw = self.r.hgetall(key)
        if not raw:
            return

        pipe = self.r.pipeline()
        for motif_id, amp_str in raw.items():
            new_amp = float(amp_str) * decay_factor
            if new_amp < 0.001:
                pipe.hdel(key, motif_id)
            else:
                pipe.hset(key, motif_id, str(round(new_amp, 6)))
        pipe.execute()

    # --- Tile Motif Cache ---

    def tile_cache_put(self, tile_id: str, assignments: List[MotifAssignment]):
        """Cache motif assignments for a tile."""
        data = [
            {
                "motif_id": a.motif_id,
                "amp": a.amp,
                "phase": a.phase,
                "confidence": a.confidence,
                "source": a.source,
            }
            for a in assignments
        ]
        self.r.set(
            f"{PREFIX}tile:{tile_id}:motifs",
            json.dumps(data),
            ex=86400 * 7,  # 7 day TTL
        )

    def tile_cache_get(self, tile_id: str) -> Optional[List[Dict]]:
        """Get cached motif assignments for a tile."""
        raw = self.r.get(f"{PREFIX}tile:{tile_id}:motifs")
        if raw:
            return json.loads(raw)
        return None

    # --- Working Set (per session) ---

    def ws_add(self, session_id: str, item_id: str):
        """Add item to session working set."""
        self.r.sadd(f"{PREFIX}ws:{session_id}", item_id)

    def ws_get(self, session_id: str) -> Set[str]:
        """Get session working set."""
        return self.r.smembers(f"{PREFIX}ws:{session_id}")

    def ws_clear(self, session_id: str):
        """Clear session working set."""
        self.r.delete(f"{PREFIX}ws:{session_id}")

    # --- Gate Snapshot ---

    def gate_snapshot_put(self, snapshot: Dict):
        """Store most recent gate evaluation."""
        self.r.set(f"{PREFIX}gate:last", json.dumps(snapshot))

    def gate_snapshot_get(self) -> Optional[Dict]:
        """Get most recent gate evaluation."""
        raw = self.r.get(f"{PREFIX}gate:last")
        if raw:
            return json.loads(raw)
        return None

    # --- Anchor Vectors ---

    def anchor_put(self, anchor_type: str, anchor_id: str, vector: List[float]):
        """Store an anchor vector. Type is 'motif' or 'theme'."""
        self.r.set(
            f"{PREFIX}anchor:{anchor_type}:{anchor_id}",
            json.dumps(vector),
        )

    def anchor_get(self, anchor_type: str, anchor_id: str) -> Optional[List[float]]:
        """Get an anchor vector by type and id."""
        raw = self.r.get(f"{PREFIX}anchor:{anchor_type}:{anchor_id}")
        if raw:
            return json.loads(raw)
        return None

    def anchor_get_all(self, anchor_type: str) -> Dict[str, List[float]]:
        """Get all anchor vectors of a given type."""
        result = {}
        for key in self.r.scan_iter(f"{PREFIX}anchor:{anchor_type}:*", count=100):
            # key is like "hmm:anchor:motif:HMM.JOY_BASELINE"
            anchor_id = key.split(f"anchor:{anchor_type}:", 1)[1]
            raw = self.r.get(key)
            if raw:
                result[anchor_id] = json.loads(raw)
        return result

    # --- Stats ---

    def stats(self) -> Dict[str, int]:
        """Get stats about HMM Redis data."""
        # Count inverted index keys
        inv_keys = list(self.r.scan_iter(f"{PREFIX}inv:*", count=1000))
        # Count tile cache keys
        tile_keys = list(self.r.scan_iter(f"{PREFIX}tile:*", count=1000))
        # Get field sizes
        field_sizes = {}
        for k in range(3):
            field_sizes[f"field_{k}"] = self.r.hlen(f"{PREFIX}field:{k}")

        return {
            "inverted_index_motifs": len(inv_keys),
            "tile_cache_entries": len(tile_keys),
            **field_sizes,
        }

    def wipe(self):
        """Delete all HMM keys from Redis (for rebuild)."""
        for key in self.r.scan_iter(f"{PREFIX}*", count=1000):
            self.r.delete(key)
