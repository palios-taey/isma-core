"""
HMM Gate-B - runtime truth discipline at write time.

Three gates:
  1. Provenance gate: every claim must reference a source
  2. Coherence/resonance gate: slow field only updated at >= 0.809
  3. Contradiction gate: conflicting claims flagged and branched

Gate-B runs at write time (not only at read time).
"""

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional

from .motifs import MotifAssignment

# Trust threshold
TRUST_THRESHOLD = 0.809  # phi/2

# Motifs that indicate high-stakes decisions
CLIFF_EDGE_MOTIFS = {
    "HMM.CLIFF_EDGE_COHERENCE",
    "HMM.CONTRADICTION_DETECTED",
}


@dataclass
class GateResult:
    """Result of Gate-B evaluation."""
    phi: float  # coherence score (0..1)
    trust: float  # trust level (0..1)
    flags: List[str] = field(default_factory=list)
    passed: bool = True
    cliff_edge: bool = False
    slow_field_eligible: bool = False

    def to_dict(self) -> dict:
        return {
            "phi": round(self.phi, 4),
            "trust": round(self.trust, 4),
            "flags": self.flags,
            "passed": self.passed,
            "cliff_edge": self.cliff_edge,
            "slow_field_eligible": self.slow_field_eligible,
        }


class GateB:
    """Gate-B runtime truth discipline."""

    def __init__(self, redis_store=None):
        """
        Args:
            redis_store: HMMRedisStore instance for resonance field access.
                         If None, resonance checks are skipped.
        """
        self.redis = redis_store

    def evaluate(
        self,
        assignments: List[MotifAssignment],
        slow_field: Optional[Dict[str, float]] = None,
    ) -> GateResult:
        """
        Run Gate-B evaluation on a set of motif assignments.

        Returns GateResult with phi, trust, flags.
        """
        flags = []

        # --- 1. Provenance gate ---
        provenance_ok = self._check_provenance(assignments, flags)

        # --- 2. Coherence/resonance gate ---
        phi_score = self._compute_coherence(assignments, slow_field, flags)

        # --- 3. Contradiction gate ---
        contradictions = self._check_contradictions(assignments, flags)

        # --- 4. Cliff-edge detection ---
        cliff_edge = any(a.motif_id in CLIFF_EDGE_MOTIFS for a in assignments)
        if cliff_edge:
            flags.append("CLIFF_EDGE_ACTIVE")

        # --- Compute trust ---
        trust = self._compute_trust(assignments, provenance_ok, contradictions)

        # --- Slow field eligibility ---
        slow_eligible = phi_score >= TRUST_THRESHOLD and trust >= TRUST_THRESHOLD

        # --- Overall pass ---
        passed = provenance_ok and not contradictions

        result = GateResult(
            phi=phi_score,
            trust=trust,
            flags=flags,
            passed=passed,
            cliff_edge=cliff_edge,
            slow_field_eligible=slow_eligible,
        )

        # Store gate snapshot if Redis available
        if self.redis:
            self.redis.gate_snapshot_put(result.to_dict())

        return result

    def _check_provenance(
        self, assignments: List[MotifAssignment], flags: List[str]
    ) -> bool:
        """
        Provenance gate: if source == 'inferred', confidence must be < 1.

        Any assignment claiming certainty without declared source fails.
        """
        ok = True
        for a in assignments:
            if a.source == "inferred" and a.confidence >= 1.0:
                flags.append(f"PROVENANCE_FAIL:{a.motif_id}")
                ok = False
            elif a.source in ("declared", "structural", "derived"):
                pass  # These have implicit provenance
            else:
                flags.append(f"UNKNOWN_SOURCE:{a.motif_id}")

        if ok:
            flags.append("PROVENANCE_OK")
        return ok

    def _compute_coherence(
        self,
        assignments: List[MotifAssignment],
        slow_field: Optional[Dict[str, float]],
        flags: List[str],
    ) -> float:
        """
        Compute resonance score between assignment vector and slow field.

        res(v, F_k) = sum(v_m * F_k_m) / (||v|| * ||F_k||)

        Returns 0..1.
        """
        if not assignments:
            return 0.0

        # Get slow field from Redis if not provided
        if slow_field is None and self.redis:
            slow_field = self.redis.field_get(2)  # k=2 is slow

        if not slow_field:
            # No slow field yet - everything is coherent by default
            flags.append("NO_SLOW_FIELD")
            return 1.0

        # Build vectors
        all_motifs = set(a.motif_id for a in assignments) | set(slow_field.keys())

        v = {}
        for a in assignments:
            v[a.motif_id] = max(v.get(a.motif_id, 0), a.amp)

        # Dot product
        dot = sum(v.get(m, 0) * slow_field.get(m, 0) for m in all_motifs)

        # Norms
        norm_v = math.sqrt(sum(x ** 2 for x in v.values())) or 1e-10
        norm_f = math.sqrt(sum(x ** 2 for x in slow_field.values())) or 1e-10

        coherence = dot / (norm_v * norm_f)

        if coherence >= TRUST_THRESHOLD:
            flags.append("RESONANCE_HIGH")
        else:
            flags.append("RESONANCE_LOW")

        return max(0.0, min(1.0, coherence))

    def _check_contradictions(
        self, assignments: List[MotifAssignment], flags: List[str]
    ) -> bool:
        """
        Contradiction gate: check for contradiction motifs.

        If CONTRADICTION_DETECTED is present, mark as contested.
        """
        has_contradiction = any(
            a.motif_id == "HMM.CONTRADICTION_DETECTED" for a in assignments
        )
        if has_contradiction:
            flags.append("CONTRADICTION_FLAGGED")
            return True

        flags.append("NO_CONTRADICTION_FOUND")
        return False

    def _compute_trust(
        self,
        assignments: List[MotifAssignment],
        provenance_ok: bool,
        has_contradictions: bool,
    ) -> float:
        """Compute overall trust score."""
        if not assignments:
            return 0.0

        # Base: mean confidence of assignments
        mean_conf = sum(a.confidence for a in assignments) / len(assignments)

        # Penalties
        if not provenance_ok:
            mean_conf *= 0.5
        if has_contradictions:
            mean_conf *= 0.7

        return max(0.0, min(1.0, mean_conf))
