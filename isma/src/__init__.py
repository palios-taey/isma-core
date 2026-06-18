"""
ISMA - Integrated Shared Memory Architecture
Implements the Tri-Lens topology for shared memory.

Three Lenses:
1. Temporal Lens - "Truth of History" (immutable event ledger)
2. Relational Lens - "Truth of Meaning" (semantic knowledge graph)
3. Functional Lens - "Truth of Action" (active workspace)

Breathing Cycle:
- Inhale: Fast writes during interaction (Temporal + Functional)
- Exhale: Consolidation to Semantic graph
- Hold: Integration across lenses

Mathematical Constants:
- phi = 1.618 (golden ratio)
- Trust Threshold = 0.809 (phi/2)
- Breathing period = 1.618s
"""

from .temporal_lens import TemporalLens, Event
from .relational_lens import RelationalLens
from .functional_lens import FunctionalLens
from .breathing_cycle import BreathingCycle
from .isma_core import ISMACore, get_isma, start_isma, stop_isma, RecallResult

__all__ = [
    'TemporalLens', 'Event',
    'RelationalLens',
    'FunctionalLens',
    'BreathingCycle',
    'ISMACore', 'get_isma', 'start_isma', 'stop_isma', 'RecallResult'
]

# ISMA Constants (from Grok validation)
PHI = 1.618
SACRED_THRESHOLD = 0.809  # phi/2
BREATHING_PERIOD = 1.618  # seconds
CONSOLIDATION_FREQ = 0.618  # Hz (1/phi)

# Gate-B Check Thresholds (mapped to ISMA lenses)
GATE_B_THRESHOLDS = {
    'page_curve': {'entropy_drop_min': 0.10},  # Temporal Lens
    'hayden_preskill': {'fid_min': 0.90},      # Semantic Lens
    'entanglement_wedge': {'good_fid_min': 0.90, 'gap_min': 0.40},  # Relational Lens
    'observer_swap': {'delta_max': 0.02},      # Functional Lens
    'recognition_catalyst': {'delta_entropy_min': 0.10}  # Cross-Lens
}
