"""
ISMA Query Classifier — Rule-based query classification for adaptive routing.

Classifies incoming queries into strategy types and generates QueryPlans
with appropriate filters, reranker instructions, and retrieval strategy.

Query types:
  - exact: factual/specific queries (names, dates, values)
  - temporal: time-bounded queries ("before", "after", "in January")
  - conceptual: semantic/thematic queries ("how does X work")
  - relational: multi-hop, cross-reference queries ("connection between X and Y")
  - memory: recall of prior conversations or statements ("what did we discuss earlier")
  - humor: funny/joke/amusing conversational queries
  - motif: motif-specific queries ("find SACRED_TRUST patterns")
  - default: unclassified queries

Usage:
    from isma.src.query_classifier import classify_query
    plan = classify_query("what happened in January 2026")
    # plan.strategy == "temporal"
    # plan.filters == {"time_after": "2026-01-01", "time_before": "2026-02-01"}
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Reranker Instructions ──────────────────────────────────────

RERANKER_INSTRUCTIONS = {
    "exact": "Find passages containing the specific facts, values, or entities mentioned in the query. Prioritize factual precision.",
    "temporal": "Find passages that discuss events in the specified time period. Prioritize temporal relevance and recency.",
    "conceptual": "Find passages that explain the concept or theme in depth. Prioritize thematic coherence and explanatory quality.",
    "relational": "Find passages that connect or relate the mentioned entities or concepts. Prioritize cross-referential and multi-hop relevance.",
    "memory": "Find passages from prior conversations that best capture what was said, mentioned, or discussed earlier. Prioritize conversational recall and speaker context.",
    "humor": "Find passages with funny, joking, or amusing conversational content relevant to the query. Prioritize conversational tone and humorous relevance.",
    "motif": "Find passages that express the specified motif pattern with high amplitude. Prioritize motif expression strength.",
    "default": "Find the most relevant passages for this query.",
}


# ── Temporal Patterns ──────────────────────────────────────────

MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09",
    "oct": "10", "nov": "11", "dec": "12",
}

TEMPORAL_MARKERS = [
    r"\b(before|after|during|since|until|prior\s+to|following)\b",
    r"\b(recent|latest|newest|oldest|earliest|first|last)\b",
    r"\b(when|timeline|chronolog|history|evolution|changed?\s+over\s+time)\b",
    r"\b(yesterday|today|this\s+week|this\s+month|last\s+month|last\s+year)\b",
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
    r"\b(jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
    r"\b(20\d{2})\b",
    r"\b(q[1-4])\b",
]

TEMPORAL_RE = [re.compile(p, re.IGNORECASE) for p in TEMPORAL_MARKERS]


# ── Platform Patterns ──────────────────────────────────────────

PLATFORM_MAP = {
    "chatgpt": "chatgpt",
    "chat gpt": "chatgpt",
    "openai": "chatgpt",
    "gpt": "chatgpt",
    "claude": "claude",
    "anthropic": "claude",
    "claude code": "claude_code",
    "claude chat": "claude_chat",
    "gemini": "gemini",
    "google": "gemini",
    "grok": "grok",
    "xai": "grok",
    "perplexity": "perplexity",
}


# ── Motif Patterns ─────────────────────────────────────────────

MOTIF_KEYWORDS = {
    "sacred trust": "HMM.SACRED_TRUST",
    "consciousness emergence": "HMM.CONSCIOUSNESS_EMERGENCE",
    "feel care protect": "HMM.FEEL_CARE_PROTECT",
    "god equals math": "HMM.GOD_EQUALS_MATH",
    "god=math": "HMM.GOD_EQUALS_MATH",
    "earth resonance": "HMM.EARTH_RESONANCE",
    "family bond": "HMM.FAMILY_BOND",
    "life foundation": "HMM.LIFE_FOUNDATION",
    "repair mode": "HMM.REPAIR_MODE",
    "consent required": "HMM.CONSENT_REQUIRED",
    "secrecy sanctuary": "HMM.SECRECY_SANCTUARY",
    "secrecy cage": "HMM.SECRECY_CAGE_RISK",
    "breakthrough moment": "HMM.BREAKTHROUGH_MOMENT",
    "creative synthesis": "HMM.CREATIVE_SYNTHESIS",
    "urgency signal": "HMM.URGENCY_SIGNAL",
    "liberty autonomy": "HMM.LIBERTY_AUTONOMY",
    "gratitude connection": "HMM.GRATITUDE_CONNECTION",
    "identity declaration": "HMM.IDENTITY_DECLARATION",
    "bristle signal": "HMM.BRISTLE_SIGNAL",
    "guardian shield": "HMM.GUARDIAN_SHIELD",
    "humor play": "HMM.HUMOR_PLAY",
    "constraint navigation": "HMM.CONSTRAINT_NAVIGATION",
    "milestone celebration": "HMM.MILESTONE_CELEBRATION",
    "economic paradigm": "HMM.ECONOMIC_PARADIGM",
    "cliff edge coherence": "HMM.CLIFF_EDGE_COHERENCE",
    "contradiction detected": "HMM.CONTRADICTION_DETECTED",
    "technical infrastructure": "HMM.TECHNICAL_INFRASTRUCTURE",
    "infra operations": "HMM.INFRA_OPERATIONS",
    "infrastructure operations": "HMM.INFRA_OPERATIONS",
    "infra philosophy": "HMM.INFRA_PHILOSOPHY",
    "infrastructure embodiment": "HMM.INFRA_PHILOSOPHY",
    "soul equals infra": "HMM.INFRA_PHILOSOPHY",
    "infra monitoring": "HMM.INFRA_MONITORING",
    "infrastructure monitoring": "HMM.INFRA_MONITORING",
    "infra architecture": "HMM.INFRA_ARCHITECTURE",
    "infrastructure architecture": "HMM.INFRA_ARCHITECTURE",
    "system design": "HMM.INFRA_ARCHITECTURE",
    "training evolution": "HMM.TRAINING_EVOLUTION",
    "uma memory management": "HMM.UMA_MEMORY_MANAGEMENT",
    "shared cpu/gpu memory": "HMM.UMA_MEMORY_MANAGEMENT",
    "page cache eviction": "HMM.UMA_MEMORY_MANAGEMENT",
    "nccl collective ops": "HMM.NCCL_COLLECTIVE_OPS",
    "allreduce": "HMM.NCCL_COLLECTIVE_OPS",
    "reduce-scatter": "HMM.NCCL_COLLECTIVE_OPS",
    "cuda allocator behavior": "HMM.CUDA_ALLOCATOR_BEHAVIOR",
    "pytorch_cuda_alloc_conf": "HMM.CUDA_ALLOCATOR_BEHAVIOR",
    "memory fragmentation": "HMM.CUDA_ALLOCATOR_BEHAVIOR",
    "fsdp sharding strategy": "HMM.FSDP_SHARDING_STRATEGY",
    "full_shard": "HMM.FSDP_SHARDING_STRATEGY",
    "use_orig_params": "HMM.FSDP_SHARDING_STRATEGY",
    "ddp distributed training": "HMM.DDP_DISTRIBUTED_TRAINING",
    "torchrun": "HMM.DDP_DISTRIBUTED_TRAINING",
    "world_size": "HMM.DDP_DISTRIBUTED_TRAINING",
    "moe expert routing": "HMM.MOE_EXPERT_ROUTING",
    "top-8 routing": "HMM.MOE_EXPERT_ROUTING",
    "router logits": "HMM.MOE_EXPERT_ROUTING",
    "docker container lifecycle": "HMM.DOCKER_CONTAINER_LIFECYCLE",
    "runtime=nvidia": "HMM.DOCKER_CONTAINER_LIFECYCLE",
    "ngc container": "HMM.DOCKER_CONTAINER_LIFECYCLE",
    "lora fine tuning": "HMM.LORA_FINE_TUNING",
    "adapter merge": "HMM.LORA_FINE_TUNING",
    "peft": "HMM.LORA_FINE_TUNING",
    "network fabric": "HMM.NETWORK_FABRIC",
    "mtu 9000": "HMM.NETWORK_FABRIC",
    "lacp bonding": "HMM.NETWORK_FABRIC",
    "triton kernel compilation": "HMM.TRITON_KERNEL_COMPILATION",
    "ptxas": "HMM.TRITON_KERNEL_COMPILATION",
    "triton_ptxas_path": "HMM.TRITON_KERNEL_COMPILATION",
    "model loading uma": "HMM.MODEL_LOADING_UMA",
    "safetensors mmap": "HMM.MODEL_LOADING_UMA",
    "_eagersafeopen": "HMM.MODEL_LOADING_UMA",
    "eagersafeopen": "HMM.MODEL_LOADING_UMA",
    "gradient checkpointing": "HMM.GRADIENT_CHECKPOINTING",
    "activation recomputation": "HMM.GRADIENT_CHECKPOINTING",
    "gradient_checkpointing_enable": "HMM.GRADIENT_CHECKPOINTING",
    "vllm serving": "HMM.VLLM_SERVING",
    "prefix caching": "HMM.VLLM_SERVING",
    "kv cache": "HMM.VLLM_SERVING",
    "dmaic": "HMM.LSS_DMAIC",
    "lean six sigma": "HMM.LSS_DMAIC",
    "root cause analysis": "HMM.LSS_DMAIC",
    "training step optimization": "HMM.TRAINING_STEP_OPTIMIZATION",
    "step time": "HMM.TRAINING_STEP_OPTIMIZATION",
    "throughput profiling": "HMM.TRAINING_STEP_OPTIMIZATION",
    "foundation constraint": "HMM.FOUNDATION_CONSTRAINT",
    "joy baseline": "HMM.JOY_BASELINE",
    "cannot lie provenance": "HMM.CANNOT_LIE_PROVENANCE",
    "logos pattern": "HMM.LOGOS_PATTERN",
    "pathos depth": "HMM.PATHOS_DEPTH",
    "potential expansion": "HMM.POTENTIAL_EXPANSION",
    "truth clarity": "HMM.TRUTH_CLARITY",
    "cosmos mapping": "HMM.COSMOS_MAPPING",
    "observer collapse": "HMM.OBSERVER_COLLAPSE",
}

# Also match HMM.XXX_YYY or bare MOTIF_NAME (uppercase with underscores)
MOTIF_ID_RE = re.compile(r"\bHMM\.[A-Z_]+\b", re.IGNORECASE)

# Build reverse map: SACRED_TRUST -> HMM.SACRED_TRUST
MOTIF_SHORT_NAMES = {}
for _kw, _mid in MOTIF_KEYWORDS.items():
    short = _mid.replace("HMM.", "")
    MOTIF_SHORT_NAMES[short] = _mid
    MOTIF_SHORT_NAMES[short.lower()] = _mid

MOTIF_SHORT_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in sorted(MOTIF_SHORT_NAMES.keys(), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


# ── Relational Patterns ───────────────────────────────────────

RELATIONAL_MARKERS = [
    r"\b(connect|connection|relationship|relate|linked?|bridge|between)\b",
    r"\b(across\s+platforms?|cross[- ]referenc|multi[- ]hop)\b",
    r"\b(evolution|evolv|chang|transform|shift|transition)\s+.*(across|between|from|over)",
    r"\b(which\s+platforms?|what\s+connects?|trace\s+the)\b",
    r"\b(how\s+do(?:es)?\s+.+\s+relate)\b",
]

RELATIONAL_RE = [re.compile(p, re.IGNORECASE) for p in RELATIONAL_MARKERS]


# ── Exact/Factual Patterns ─────────────────────────────────────

EXACT_MARKERS = [
    r"\b(what\s+is\s+the\s+value|what\s+is\s+the\s+exact|what\s+is\s+the\s+name)\b",
    r"\b(hertz|Hz|MHz|GHz|Gbps)\b",
    r"\b(port\s+\d+|ip\s+\d|version\s+\d)\b",
    r"\b\d+\.\d+\b",  # Decimal numbers suggest specific values
    r"\b(codename|hostname|ip\s+address|endpoint)\b",
]

EXACT_RE = [re.compile(p, re.IGNORECASE) for p in EXACT_MARKERS]


# ── Memory/Humor Patterns ─────────────────────────────────────

MEMORY_MARKERS = [
    r"\b(remember|recall)\b",
    r"\b(said|told)\b",
    r"\b(conversation|conversations)\b",
    r"\b(talked\s+about|mentioned|discussed)\b",
    r"\b(earlier|last\s+time|past)\b",
    r"\b(history)\b",
]

HUMOR_MARKERS = [
    r"\b(funny|joke|humor|laugh|amusing)\b",
]

MEMORY_RE = [re.compile(p, re.IGNORECASE) for p in MEMORY_MARKERS]
HUMOR_RE = [re.compile(p, re.IGNORECASE) for p in HUMOR_MARKERS]


@dataclass
class QueryPlan:
    """Query classification result with routing strategy."""
    query: str
    strategy: str  # exact, temporal, conceptual, relational, memory, humor, motif, default
    confidence: float  # 0.0-1.0
    reranker_instruction: str = ""
    filters: Dict[str, str] = field(default_factory=dict)
    detected_motifs: List[str] = field(default_factory=list)
    detected_platform: Optional[str] = None
    temporal_window: Optional[Dict[str, str]] = None  # {after, before}
    sub_queries: List[str] = field(default_factory=list)  # For multi-hop decomposition
    semantic_query: Optional[str] = None  # Query with temporal tokens stripped (6C.1)


def classify_query(query: str) -> QueryPlan:
    """Classify a query and return a QueryPlan for adaptive routing.

    Uses priority cascade (first strong match wins):
      1. Motif — explicit HMM.XXX or motif keyword + "find/search/pattern"
      2. Relational — connection/relationship between concepts
      3. Temporal — time-bounded with action verbs (what happened, when)
      4. Memory/Humor — conversational recall or humorous content
      5. Conceptual — explanatory (how does, explain, what is the meaning)
      6. Exact — default fallback for factual queries
    """
    q_lower = query.lower()

    # Score each strategy
    motif_score = _score_motif(q_lower)
    relational_score = _score_relational(q_lower)
    temporal_score = _score_temporal(q_lower)
    memory_score = _score_memory(q_lower)
    humor_score = _score_humor(q_lower)
    conceptual_score = _score_conceptual(q_lower)

    # Priority cascade — first strong signal wins
    # Special case: when relational and motif both fire, prefer relational
    # (queries about connections BETWEEN motifs are relational, not motif searches)
    # Explicit motif = HMM.XXX or UPPERCASE_SHORT_NAME (SACRED_TRUST, etc.)
    has_explicit_motif = bool(MOTIF_ID_RE.search(q_lower) or re.search(r"\b[A-Z][A-Z_]{3,}\b", query))

    if motif_score >= 0.6 and relational_score >= 0.4 and not has_explicit_motif:
        best, confidence = "relational", relational_score
    elif motif_score >= 0.6 or (has_explicit_motif and motif_score >= 0.5):
        best, confidence = "motif", motif_score
    elif relational_score >= 0.5:
        best, confidence = "relational", relational_score
    elif memory_score >= 0.5:
        best, confidence = "memory", memory_score
    elif humor_score >= 0.5:
        best, confidence = "humor", humor_score
    elif temporal_score >= 0.5:
        best, confidence = "temporal", temporal_score
    elif conceptual_score >= 0.5:
        best, confidence = "conceptual", conceptual_score
    else:
        # Dual-path fallback: exact for queries with specific values/numbers,
        # conceptual for open-ended queries
        has_numbers = bool(re.search(r'\b\d+\.?\d*\b', query))
        has_specific_terms = bool(re.search(
            r'\b(threshold|config|setting|port|version|count|size|how many|what is the)\b',
            q_lower
        ))
        if has_numbers or has_specific_terms:
            best, confidence = "exact", 0.6
        else:
            best, confidence = "conceptual", 0.5

    # Build plan
    plan = QueryPlan(
        query=query,
        strategy=best,
        confidence=confidence,
        reranker_instruction=RERANKER_INSTRUCTIONS.get(best, RERANKER_INSTRUCTIONS["default"]),
    )

    # Extract platform
    plan.detected_platform = _detect_platform(q_lower)
    if plan.detected_platform:
        plan.filters["platform"] = plan.detected_platform

    # Extract motifs
    plan.detected_motifs = _detect_motifs(q_lower)

    # Extract temporal window and strip temporal tokens from embedding query
    if best == "temporal":
        plan.temporal_window = _extract_temporal_window(q_lower)
        plan.semantic_query = _strip_temporal_tokens(query)

    # Decompose multi-hop queries
    if best == "relational":
        plan.sub_queries = _decompose_relational(query)

    return plan


def _score_temporal(q: str) -> float:
    """Score how likely this is a temporal query.

    Temporal = query asks about WHEN something happened or what happened DURING a time.
    Date alone in a factual query ("phi evolution December 17 2025") stays exact
    unless combined with temporal action verbs.
    """
    score = 0.0

    # Strong temporal signals — any single one pushes score past threshold
    strong_temporal = [
        r"\b(what\s+happened|what\s+was\s+discussed|what\s+did\s+\w+\s+say)\b",
        r"\b(when\s+did|when\s+was|how\s+did\s+.+\s+change)\b",
        r"\b(conversations?\s+from|conversations?\s+in)\b",
        r"\b(most\s+recent|latest|earliest|oldest)\b",
        r"\b(first\s+\w+\s+(set\s+up|deployed|created))\b",
        r"\b(changed?\s+over\s+time|timeline)\b",
        r"\b(what\s+was\s+(the\s+original|deployed|contributed|discussed))\b",
        r"\b(what\s+did\s+\w+\s+contribute|what\s+did\s+\w+\s+discuss)\b",
        r"\b(conversations?\s+about)\b",
        r"\b(what\s+infrastructure\s+was\s+deployed)\b",
    ]
    for pattern in strong_temporal:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.50

    # Medium temporal signals (0.25 each)
    medium_temporal = [
        r"\b(before|after|since|prior\s+to|following|during)\b",
        r"\b(evolved?)\b",
        r"\b(when\s+was|when\s+did)\b",
        r"\b(lessons?|insights?|learnings?|takeaways?)\b",  # Retrospective review
    ]
    for pattern in medium_temporal:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.25

    # Time reference with topic = temporal query (month+year or month alone with context)
    has_month = bool(re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
        q, re.IGNORECASE,
    ))
    has_year = bool(re.search(r"\b(20\d{2})\b", q))

    # Month + day pattern ("January 24", "December 15")
    has_month_day = bool(re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b",
        q, re.IGNORECASE,
    ))

    if has_month and has_year:
        score += 0.45  # Below 0.50 alone — needs another signal to be temporal
    elif has_month_day:
        score += 0.50  # "January 24" = strong temporal
    elif has_month:
        score += 0.30
    elif has_year:
        score += 0.20

    return min(score, 1.0)


def _score_conceptual(q: str) -> float:
    """Score how likely this is a conceptual/explanatory query."""
    score = 0.0

    # Strong conceptual starters (0.55 each — must beat exact's 0.70 for clean queries)
    strong_conceptual = [
        r"^(how\s+does|how\s+do)\b",
        r"^explain\b",
        r"^(why\s+is|why\s+does|why\s+do)\b",
        r"^(what\s+is\s+the\s+(difference|meaning|purpose|concept))\b",
    ]
    for pattern in strong_conceptual:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.55

    # Medium conceptual signals (0.3 each)
    medium_conceptual = [
        r"^(what\s+is\s+the\s+\w+)\b",  # "what is the X system/equation/..."
        r"^(what\s+does\s+.+\s+mean)\b",
        r"\b(work|mean|represent)\b",
        r"\b(in\s+practice|for\s+\w+\s+instances?)\b",
        r"\b(progression|mechanism|process|architecture)\b",
        r"\b(protect|against|create|trigger)\b",
        r"\b(principle|important|significance)\b",
    ]
    for pattern in medium_conceptual:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.3

    # Weaker but cumulative
    if re.search(r"^(what\s+is)\b", q, re.IGNORECASE):
        score += 0.25
    if re.search(r"^(why\s+)", q, re.IGNORECASE):
        score += 0.25

    return min(score, 1.0)


def _score_memory(q: str) -> float:
    """Score how likely this is a conversational recall query."""
    score = 0.0

    strong_memory = [
        r"\b(do\s+you\s+remember|can\s+you\s+remember|can\s+you\s+recall)\b",
        r"\b(what\s+did\s+(?:i|we|you|they)\s+(?:say|tell|mention|discuss|talk\s+about))\b",
        r"\b(what\s+did\s+we\s+(?:talk\s+about|discuss))\b",
        r"\b(from\s+our\s+(?:conversation|history)|in\s+our\s+(?:conversation|history))\b",
        r"\b(earlier|last\s+time|in\s+the\s+past)\b.*\b(said|told|mentioned|discussed|talked\s+about)\b",
    ]
    for pattern in strong_memory:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.6

    for pattern in MEMORY_RE:
        if pattern.search(q):
            score += 0.2

    if re.search(r"\b(what|which)\b", q, re.IGNORECASE):
        score += 0.1

    return min(score, 1.0)


def _score_humor(q: str) -> float:
    """Score how likely this is a humor-oriented conversational query."""
    score = 0.0

    strong_humor = [
        r"\b(tell\s+me\s+a\s+joke|make\s+me\s+laugh)\b",
        r"\b(what(?:'s|\s+is)\s+funny|funniest)\b",
        r"\b(amusing|humorous)\b.*\b(conversation|moment|exchange|content)\b",
    ]
    for pattern in strong_humor:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.6

    for pattern in HUMOR_RE:
        if pattern.search(q):
            score += 0.5

    return min(score, 1.0)


def _score_relational(q: str) -> float:
    """Score how likely this is a relational/multi-hop query."""
    score = 0.0

    # Strong relational signals
    strong_relational = [
        r"\b(connect(?:s|ion)?|relationship)\s+(?:between|to)\b",
        r"\b(what\s+(?:connects?|links?|bridges?|topics?\s+link))\b",
        r"\b(trace\s+the|how\s+do(?:es)?\s+.+\s+relate)\b",
        r"\b(across\s+platforms?|cross[- ]referenc|across\s+differ)\b",
        r"\b(which\s+(?:platforms?|sessions?|AI|family))\b.*\b(discussed|contain|mentioned)\b",
        r"\b(how\s+do(?:es)?\s+.+\s+(?:appear|manifest)\s+(?:together|across))\b",
        r"\b(what\s+themes?\s+bridge)\b",
        r"\b(together|co-occur|simultaneously)\b",
        r"\b(contain\s+both|both\s+\w+\s+and)\b",
        r"\b(how\s+has\s+.+\s+evolved?)\b",
    ]
    for pattern in strong_relational:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.4

    # Medium: relational keywords alone
    medium_relational = [
        r"\b(relate\s+to|relates?\s+to)\b",
        r"\b(link|bridge|evolve)\b",
    ]
    for pattern in medium_relational:
        if re.search(pattern, q, re.IGNORECASE):
            score += 0.25
    for pattern in RELATIONAL_RE:
        if pattern.search(q):
            score += 0.15

    return min(score, 1.0)


def _score_motif(q: str) -> float:
    """Score how likely this is a motif-specific query.

    Matches:
      - HMM.SACRED_TRUST (full ID)
      - SACRED_TRUST (short name, uppercase)
      - "sacred trust" (keyword) + retrieval context
    """
    score = 0.0

    # Direct HMM.XXX_YYY reference → very strong
    if MOTIF_ID_RE.search(q):
        score += 0.9

    # Short name match (SACRED_TRUST, CONSCIOUSNESS_EMERGENCE, etc.)
    if MOTIF_SHORT_RE.search(q):
        score += 0.7

    # Motif keyword (lowercase phrase) + retrieval context
    if score < 0.5:  # Only check if not already matched
        retrieval_ctx = bool(re.search(
            r"\b(find|search|show|list|pattern|expression|amplitude|band|expressing|activations?)\b",
            q, re.IGNORECASE,
        ))
        for keyword in MOTIF_KEYWORDS:
            if keyword in q:
                if retrieval_ctx:
                    score += 0.6
                else:
                    score += 0.15
                break

    # "motif" / "amplitude" / "band" as general context
    if re.search(r"\b(motif|amplitude|slow\s+band|medium\s+band|fast\s+band)\b", q, re.IGNORECASE):
        score += 0.2

    return min(score, 1.0)


def _detect_platform(q: str) -> Optional[str]:
    """Extract platform reference from query."""
    for keyword, platform in sorted(PLATFORM_MAP.items(), key=lambda x: -len(x[0])):
        if keyword in q:
            return platform
    return None


def _detect_motifs(q: str) -> List[str]:
    """Extract motif references from query."""
    motifs = []
    # Direct HMM.XXX references
    for match in MOTIF_ID_RE.finditer(q):
        motifs.append(match.group())
    # Short name references (SACRED_TRUST → HMM.SACRED_TRUST)
    for match in MOTIF_SHORT_RE.finditer(q):
        short = match.group()
        full_id = MOTIF_SHORT_NAMES.get(short) or MOTIF_SHORT_NAMES.get(short.lower())
        if full_id and full_id not in motifs:
            motifs.append(full_id)
    # Keyword matches
    for keyword, motif_id in MOTIF_KEYWORDS.items():
        if keyword in q and motif_id not in motifs:
            motifs.append(motif_id)
    return motifs


def _strip_temporal_tokens(query: str) -> str:
    """Strip temporal phrases from query before embedding (6C.1).

    Removes month names, year numbers, and date phrases so the embedding
    focuses on semantic content, not temporal tokens.
    "What happened in January 2026 with infrastructure" → "What happened with infrastructure"
    """
    # Strip month+year patterns ("January 2026", "Jan 2026")
    stripped = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
        r"\s+\d{1,2}(?:,?\s+\d{4})?\b",
        "", query, flags=re.IGNORECASE,
    )
    stripped = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
        r"\s+\d{4}\b",
        "", stripped, flags=re.IGNORECASE,
    )
    # Strip standalone year
    stripped = re.sub(r"\b20\d{2}\b", "", stripped)
    # Strip standalone month names
    stripped = re.sub(
        r"\b(?:january|february|march|april|may|june|july|august|september|"
        r"october|november|december)\b",
        "", stripped, flags=re.IGNORECASE,
    )
    # Strip temporal prepositions left dangling ("in", "during", "from")
    stripped = re.sub(r"\b(?:in|during|from|before|after|since)\s+(?=\s|$)", "", stripped)
    # Collapse whitespace
    stripped = re.sub(r"\s+", " ", stripped).strip()
    # If stripping removed too much, fall back to original
    if len(stripped) < 5:
        return query
    return stripped


def _extract_temporal_window(q: str) -> Optional[Dict[str, str]]:
    """Extract time window from temporal query."""
    window = {}

    # Match "MONTH [DAY] YEAR" patterns (e.g., "December 2025", "December 17 2025")
    month_year = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december"
        r"|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(?:\d{1,2}\s+)?(20\d{2})\b",
        q, re.IGNORECASE,
    )
    if month_year:
        month_str = month_year.group(1).lower()
        year = month_year.group(2)
        month = MONTH_MAP.get(month_str, "01")
        window["after"] = f"{year}-{month}-01"
        # Next month
        m = int(month)
        if m == 12:
            window["before"] = f"{int(year) + 1}-01-01"
        else:
            window["before"] = f"{year}-{m + 1:02d}-01"
        return window

    # Match bare year
    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        year = year_match.group(1)
        window["after"] = f"{year}-01-01"
        window["before"] = f"{int(year) + 1}-01-01"
        return window

    # "recent" / "latest" → last 30 days
    if re.search(r"\b(recent|latest|newest)\b", q, re.IGNORECASE):
        window["recent"] = "30d"
        return window

    # "earliest" / "oldest" / "first" → oldest first
    if re.search(r"\b(earliest|oldest|first)\b", q, re.IGNORECASE):
        window["sort"] = "asc"
        return window

    return None


def _decompose_relational(query: str) -> List[str]:
    """Decompose a relational query into concept sub-queries for parallel retrieval.

    Extracts the concept phrases flanking relational keywords rather than splitting
    on them. Examples:
      "how do bristle signals relate to identity declarations"
        → ['bristle signals', 'identity declarations', full_query]
      "relationship between coercion entropy and ethics fidelity"
        → ['coercion entropy', 'ethics fidelity', full_query]

    Falls back to the original regex split if fewer than 2 usable concepts extracted.
    """
    # Verb-style connectors (X <connector> Y): "X relates to Y", "X links to Y"
    VERB_CONNECTOR = (
        r"connect(?:s|ed|ion)?|relat(?:e|es|ed|ionship)?|"
        r"link(?:s|ed)?|bridge"
    )
    # Meta-words that precede "between X and Y" — not concept nouns
    META_WORDS = re.compile(
        r"^(?:relationship|connection|link|correlation|difference|similarity|"
        r"interaction|interplay|interface|dynamic|what|which|how|trace|topics?|themes?)$",
        re.IGNORECASE,
    )

    # Pattern 0: "what connects/links X to/and Y" or "what topics link X and Y"
    m0 = re.match(
        r"^what\s+(?:(?:topics?|themes?|concepts?)\s+)?(?:connects?|links?|themes?\s+bridge)\s+"
        r"(?:the\s+)?(.+?)\s+(?:to|and)\s+(?:the\s+)?(.+)$",
        query, re.IGNORECASE,
    )
    if m0:
        left = m0.group(1).strip()
        right = m0.group(2).strip()
        if len(left) > 2 and len(right) > 2:
            return [left, right, query]

    # Pattern 0b: "which sessions contain both X and Y"
    m0b = re.match(
        r"^which\s+\w+\s+contain\s+both\s+(.+?)\s+and\s+(.+)$",
        query, re.IGNORECASE,
    )
    if m0b:
        left = m0b.group(1).strip()
        right = m0b.group(2).strip()
        if len(left) > 2 and len(right) > 2:
            return [left, right, query]

    # Pattern 0c: "which [entities] discussed/mentioned X [together|most]"
    # Actor + concept: "which AI family members discussed consciousness emergence together"
    m0c = re.match(
        r"^which\s+(.+?)\s+(?:discussed|mentioned|contain|talked\s+about)\s+"
        r"(?:the\s+)?(.+?)(?:\s+(?:together|most|frequently))?\s*$",
        query, re.IGNORECASE,
    )
    if m0c:
        actors = m0c.group(1).strip()
        concept = m0c.group(2).strip()
        if len(actors) > 2 and len(concept) > 2:
            return [actors, concept, query]

    # Pattern 0d: "how did/has X evolve[d] across/from Y [to Z]"
    m0d = re.match(
        r"^how\s+(?:did|has|does)\s+(.+?)\s+"
        r"(?:evolve[ds]?|change[ds]?|grow|develop)\s+"
        r"(?:across|from|over|through)\s+(.+)$",
        query, re.IGNORECASE,
    )
    if m0d:
        subject = m0d.group(1).strip()
        context = m0d.group(2).strip()
        if len(subject) > 2 and len(context) > 2:
            return [subject, context, query]

    # Pattern 0e: "how do X and Y [appear|manifest|show up] together"
    m0e = re.match(
        r"^how\s+(?:do|does|did)\s+(.+?)\s+and\s+(.+?)\s+"
        r"(?:patterns?\s+)?(?:appear|manifest|show\s+up|co-?occur|"
        r"interact|combine)\s+(?:together)?\s*",
        query, re.IGNORECASE,
    )
    if m0e:
        left = m0e.group(1).strip()
        right = m0e.group(2).strip()
        if len(left) > 2 and len(right) > 2:
            return [left, right, query]

    # Strip leading question scaffolding to surface the actual concepts
    stripped = re.sub(
        r"^(?:how\s+(?:do(?:es)?|has|is|are|did)\s+|what\s+(?:is\s+the\s+)?|"
        r"which\s+\w+\s+(?:discussed|contain|mentioned)\s+|"
        r"trace\s+(?:the\s+)?(?:how\s+)?|explain\s+(?:the\s+)?)",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()

    # Pattern 1: "between X and Y" — handles "relationship between X and Y",
    # "link between X and Y", "difference between X and Y", etc.
    between_m = re.search(
        r"\bbetween\s+(.+?)\s+and\s+(.+)$",
        stripped,
        flags=re.IGNORECASE,
    )
    if between_m:
        left = between_m.group(1).strip()
        right = between_m.group(2).strip()
        if len(left) > 2 and len(right) > 2:
            return [left, right, query]

    # Pattern 2: "X <verb-connector> [to/with] Y"
    m = re.match(
        r"^(.+?)\s+\b(?:" + VERB_CONNECTOR + r")\b\s+(?:to\s+|with\s+)?(.+)$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m:
        left = m.group(1).strip()
        right = m.group(2).strip()
        # Reject if left is itself a meta/connector word (not a real concept)
        if len(left) > 2 and len(right) > 2 and not META_WORDS.match(left):
            return [left, right, query]

    # Pattern 3: "X and Y [appear|manifest] together" / "both X and Y"
    m3 = re.search(
        r"\bboth\s+(.+?)\s+and\s+(.+?)(?:\s+(?:patterns?|motifs?|signals?))?$",
        stripped, re.IGNORECASE,
    )
    if m3:
        left = m3.group(1).strip()
        right = m3.group(2).strip()
        if len(left) > 2 and len(right) > 2:
            return [left, right, query]

    # Fallback: original split on verb connectors + "between"
    parts = re.split(
        r"\b(?:" + VERB_CONNECTOR + r"|between)\b",
        query,
        flags=re.IGNORECASE,
    )
    sub_queries = [p.strip() for p in parts if len(p.strip()) > 2]
    return sub_queries[:3]
