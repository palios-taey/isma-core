"""
Harmonic Motif Memory (HMM) - ISMA-native memory substrate.

HMM is a memory system where every stored fact has provenance,
every meaning is explicit (Motifs / Rosetta atoms), and retrieval
is driven by multi-timescale resonance fields, not dense-vector similarity.

Vectors may exist as an optional index. They are not the substrate.
"""

from .ids import artifact_id, tile_id, canonicalize_text
from .eventlog import EventLog, Event
from .motifs import MotifDictionary, MotifAssignment, assign_motifs
from .neo4j_store import HMMNeo4jStore
from .redis_store import HMMRedisStore
from .query import HMMQuery
from .gate_b import GateB, GateResult

__version__ = "0.1.0"
