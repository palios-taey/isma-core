#!/usr/bin/env python3
"""
ColBERT retrieval for ISMA_ColBERT_Pilot — MaxSim late-interaction search.

Used as supplementary signal fused via RRF with V1 dense+BM25 results.

Usage (standalone):
    python3 colbert_retrieval.py --query "information retrieval example" --top-k 10

Usage (as module):
    from isma.scripts.colbert_retrieval import ColBERTRetrieval, rrf_fusion
    colbert = ColBERTRetrieval()
    results = colbert.search("your query", top_k=20)
    # returns list of {"content_hash": ..., "score": ...}
"""

import os
from isma.config import WEAVIATE_URL as CONFIG_WEAVIATE_URL, NEO4J_URI as CONFIG_NEO4J_URI, REDIS_HOST as CONFIG_REDIS_HOST, REDIS_PORT as CONFIG_REDIS_PORT, EMBEDDING_URL as CONFIG_EMBEDDING_URL, ISMA_QUERY_API as CONFIG_ISMA_QUERY_API, NIGHTLY_MAC_HOST as CONFIG_NIGHTLY_MAC_HOST
import argparse
import json
import logging
import sys
import time

import requests
import torch
import torch.nn.functional as F
from transformers import AutoModel, AutoTokenizer

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

WEAVIATE_URL = CONFIG_WEAVIATE_URL
WEAVIATE_GQL = f"{WEAVIATE_URL}/v1/graphql"
WEAVIATE_REST = f"{WEAVIATE_URL}/v1"
PILOT_CLASS = "ISMA_ColBERT_Pilot"
COLBERT_MODEL = "jinaai/jina-colbert-v2"
COLBERT_DIM = 64
MAX_QUERY_TOKENS = 64


# =============================================================================
# MODEL
# =============================================================================

_model = None
_tokenizer = None


def load_model():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    log.info(f"Loading {COLBERT_MODEL}...")
    _tokenizer = AutoTokenizer.from_pretrained(COLBERT_MODEL)
    _model = AutoModel.from_pretrained(COLBERT_MODEL, trust_remote_code=True)
    _model.eval()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _model = _model.to(device)
    log.info(f"Model loaded on {device}")
    return _model, _tokenizer


def encode_query(text: str) -> list[list[float]]:
    """
    Encode a query for ColBERT MaxSim retrieval.
    Returns list of per-token 64-dim vectors (query mode, [unused0] prefix).
    """
    model, tokenizer = load_model()
    device = next(model.parameters()).device

    # ColBERT query prefix: [unused0] for queries, [unused1] for passages
    prefixed = f"[unused0]{text}"
    inputs = tokenizer(
        prefixed,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=MAX_QUERY_TOKENS,
    ).to(device)

    with torch.no_grad():
        out = model(**inputs)
        token_vecs = out.last_hidden_state[0]  # (seq_len, hidden_dim)
        # Matryoshka: use first 64 dims
        token_vecs = token_vecs[:, :COLBERT_DIM]
        # L2 normalize each token vector
        token_vecs = F.normalize(token_vecs, dim=-1)

    # Exclude padding tokens
    attention_mask = inputs["attention_mask"][0]
    token_vecs = token_vecs[attention_mask.bool()]

    return token_vecs.cpu().numpy().tolist()


# =============================================================================
# WEAVIATE MAXSIM SEARCH
# =============================================================================

def search_colbert(query_vectors: list[list[float]], top_k: int = 20) -> list[dict]:
    """
    Submit query multi-vectors to Weaviate ISMA_ColBERT_Pilot using MaxSim.
    Returns list of {"content_hash": ..., "distance": ..., "rank": ...}

    Weaviate 1.30+ multi-vector query format:
    - Use `vector` field (2D array) with `targets` sub-object (NOT vectorPerTarget + targetVectors)
    - `combinationMethod: minimum` routes through the correct multi-vector code path
    - Distances are negative (dot product); more negative = more similar
    """
    gql = f"""{{
      Get {{
        {PILOT_CLASS}(
          limit: {top_k}
          nearVector: {{
            vector: {json.dumps(query_vectors)}
            targets: {{
              targetVectors: ["colbert"]
              combinationMethod: minimum
            }}
          }}
        ) {{
          content_hash
          platform
          dominant_motifs
          _additional {{
            distance
            id
          }}
        }}
      }}
    }}"""
    try:
        r = requests.post(
            f"{WEAVIATE_URL}/v1/graphql",
            json={"query": gql},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            log.error(f"ColBERT GraphQL error: {data['errors']}")
            return []
        items = data.get("data", {}).get("Get", {}).get(PILOT_CLASS, []) or []
        results = []
        for rank, item in enumerate(items, 1):
            dist = item["_additional"].get("distance", 0.0)
            results.append({
                "content_hash": item.get("content_hash", ""),
                "distance": dist,
                "score": -dist,  # dot product: more negative distance = higher score
                "platform": item.get("platform", ""),
                "dominant_motifs": item.get("dominant_motifs", []),
                "rank": rank,
            })
        return results
    except Exception as e:
        log.error(f"ColBERT search error: {e}")
        return []


# =============================================================================
# PUBLIC API
# =============================================================================

class ColBERTRetrieval:
    """ColBERT MaxSim retrieval against ISMA_ColBERT_Pilot."""

    def __init__(self):
        # Pre-load model
        load_model()

    def search(self, query: str, top_k: int = 20) -> list[dict]:
        """
        Search ISMA_ColBERT_Pilot with MaxSim.
        Returns list of {"content_hash": ..., "score": ..., "rank": ...}
        """
        t0 = time.monotonic()
        qvecs = encode_query(query)
        results = search_colbert(qvecs, top_k=top_k)
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.debug(f"ColBERT search: {len(results)} results in {elapsed_ms:.0f}ms")
        return results

    def search_content_hashes(self, query: str, top_k: int = 20) -> list[str]:
        """Return just content_hashes in rank order."""
        return [r["content_hash"] for r in self.search(query, top_k=top_k)]


# =============================================================================
# RRF FUSION
# =============================================================================

def rrf_fusion(
    ranked_lists: list[list[str]],
    weights: list[float] = None,
    k: int = 60,
    top_n: int = 10,
) -> list[str]:
    """
    Reciprocal Rank Fusion across multiple ranked content_hash lists.

    Args:
        ranked_lists: List of ranked content_hash lists (each list is one retrieval system)
        weights: Per-list weights (default: uniform). ColBERT typically gets 0.3.
        k: RRF constant (default 60, standard value)
        top_n: Number of results to return

    Returns:
        Fused ranked list of content_hashes
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    assert len(weights) == len(ranked_lists), "weights must match ranked_lists length"

    scores: dict[str, float] = {}
    for ranked, weight in zip(ranked_lists, weights):
        for rank, content_hash in enumerate(ranked, 1):
            if content_hash:
                scores[content_hash] = scores.get(content_hash, 0.0) + weight / (k + rank)

    sorted_hashes = sorted(scores, key=scores.__getitem__, reverse=True)
    return sorted_hashes[:top_n]


# =============================================================================
# STANDALONE CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="ColBERT ISMA retrieval")
    parser.add_argument("--query", "-q", required=True)
    parser.add_argument("--top-k", type=int, default=10)
    args = parser.parse_args()

    # Check pilot collection exists
    r = requests.get(f"{WEAVIATE_URL}/v1/schema/{PILOT_CLASS}", timeout=5)
    if r.status_code != 200:
        print(f"ISMA_ColBERT_Pilot class not found. Run colbert_pilot_ingest.py --create-class --ingest first.")
        sys.exit(1)

    colbert = ColBERTRetrieval()
    t0 = time.monotonic()
    results = colbert.search(args.query, top_k=args.top_k)
    elapsed = (time.monotonic() - t0) * 1000

    print(f"\nQuery: {args.query!r}")
    print(f"ColBERT MaxSim — {len(results)} results in {elapsed:.0f}ms\n")

    for r in results:
        motifs = ", ".join(r["dominant_motifs"][:3]) if r["dominant_motifs"] else "—"
        print(f"  [{r['rank']}] {r['content_hash']} ({r['platform']}) score={r['score']:.3f}")
        print(f"       motifs: {motifs}")

    print(f"\nRRF example (ColBERT only, weight=0.3):")
    fused = rrf_fusion(
        [colbert.search_content_hashes(args.query, top_k=20)],
        weights=[0.3],
        top_n=args.top_k,
    )
    for i, h in enumerate(fused, 1):
        print(f"  [{i}] {h}")


if __name__ == "__main__":
    main()
