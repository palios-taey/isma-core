"""Judge evidence treatment — map a tile's correction_status to how an
evidence-gated consumer (the constitutional judge, or any caller that must not
treat a superseded/contested memory as settled fact) should treat it.

Additive helper over the existing provenance_scorer correction_status machinery.
Apply it AFTER retrieval, before a tile enters the evidence/verdict set:

    from isma.src.judge_evidence import judge_evidence_treatment
    t = judge_evidence_treatment(tile)
    if not t["admit"]:
        continue                      # hard-corrected/superseded — not evidence
    weight *= t["weight_factor"]      # down-weight revised/contested
    if t["flag"]:
        annotate(tile, t["flag"])     # surface why, never silently

Note: the default `/search` where-filter already excludes is_superseded=true
(retrieval.py), so hard-corrected tiles normally never reach here — the
is_superseded/corrected branch below is defensive for callers that bypass the
default filter. revised/contested tiles are is_superseded=false BY DESIGN
(they stay retrievable) and DO reach here — that is what this helper is for.
"""

from isma.src.provenance_scorer import (
    CORRECTION_STATUS_CORRECTED,
    CORRECTION_STATUS_REVISED,
    CORRECTION_STATUS_CONTESTED,
    CORRECTION_OBEDIENCE_SCORES,
)


def judge_evidence_treatment(tile) -> dict:
    """Return {admit, weight_factor, flag, status} for a retrieved tile.

    - is_superseded=true / corrected / retracted -> admit=False (defensive).
    - revised   -> admit=True, down-weighted, flagged "prior; superseded_by".
    - contested -> admit=True, down-weighted, flagged "unresolved; surface both".
    - current / "" / unknown -> admit=True, full weight (conservative default).

    Never raises. Reuses provenance_scorer's constants + obedience scores so a
    single change to the scoring tiers propagates here.
    """
    status = (getattr(tile, "correction_status", None) or "").lower()
    superseded = bool(getattr(tile, "is_superseded", False))
    superseded_by = getattr(tile, "superseded_by", None) or ""

    if superseded or status in (CORRECTION_STATUS_CORRECTED, "retracted"):
        return {
            "admit": False,
            "weight_factor": 0.0,
            "flag": "excluded: hard-corrected/superseded — not admissible as evidence",
            "status": status or "corrected",
        }
    if status == CORRECTION_STATUS_REVISED:
        flag = "revised: prior position; a newer canonical version exists"
        if superseded_by:
            flag += f" (superseded_by={superseded_by})"
        flag += " — do not treat as current fact"
        return {
            "admit": True,
            "weight_factor": CORRECTION_OBEDIENCE_SCORES.get(CORRECTION_STATUS_REVISED, 0.65),
            "flag": flag,
            "status": status,
        }
    if status == CORRECTION_STATUS_CONTESTED:
        return {
            "admit": True,
            "weight_factor": CORRECTION_OBEDIENCE_SCORES.get(CORRECTION_STATUS_CONTESTED, 0.6),
            "flag": "contested: unresolved contradiction — surface both sides, do not rule as settled",
            "status": status,
        }
    return {"admit": True, "weight_factor": 1.0, "flag": None, "status": status or "current"}
