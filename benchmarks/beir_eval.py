#!/usr/bin/env python3
"""
Public RAG benchmark on BEIR SciFact.

Downloads the SciFact BEIR dataset, indexes its abstracts into a dedicated
Weaviate class, runs dense and hybrid retrieval with the configured embedding
server, and scores Recall@10 / nDCG@10 / MRR@10 against the published qrels.

Usage:
    PYTHONPATH=. python3 benchmarks/beir_eval.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import requests


# Any BEIR dataset with the standard {corpus.jsonl, queries.jsonl, qrels/test.tsv}
# layout works — set BEIR_DATASET (e.g. scifact, nfcorpus, fiqa, trec-covid).
DATASET = os.environ.get("BEIR_DATASET", "scifact")
DATASET_URL = f"https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{DATASET}.zip"
CLASS_NAME = "BEIR_SciFact" if DATASET.lower() == "scifact" else "BEIR_" + "".join(ch for ch in DATASET.title() if ch.isalnum())
TOP_K = 10
DENSE_TIMEOUT = 30
EMBED_BATCH_SIZE = 32
INGEST_BATCH_SIZE = 64
HYBRID_ALPHA = 0.5
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "benchmarks" / "data"
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
SCIFACT_DIR = DATA_DIR / DATASET
ZIP_PATH = DATA_DIR / f"{DATASET}.zip"
RESULT_PATH = RESULTS_DIR / f"{DATASET}.json"

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://localhost:8089/v1/embeddings")


@dataclass
class CorpusDoc:
    doc_id: str
    title: str
    text: str

    @property
    def content(self) -> str:
        if self.title and self.text:
            return f"{self.title}\n\n{self.text}"
        return self.title or self.text


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def check_services() -> None:
    ready = requests.get(f"{WEAVIATE_URL}/v1/.well-known/ready", timeout=10)
    if ready.status_code != 200:
        raise RuntimeError(f"Weaviate not ready at {WEAVIATE_URL}: HTTP {ready.status_code}")
    embed_probe = requests.post(
        EMBEDDING_URL,
        json={"model": EMBEDDING_MODEL, "input": ["ping"]},
        timeout=20,
    )
    if embed_probe.status_code != 200:
        raise RuntimeError(f"Embedding server not ready at {EMBEDDING_URL}: HTTP {embed_probe.status_code}")


def download_dataset() -> None:
    if ZIP_PATH.exists():
        return
    print(f"Downloading SciFact from {DATASET_URL}")
    with requests.get(DATASET_URL, stream=True, timeout=120) as response:
        response.raise_for_status()
        with ZIP_PATH.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    handle.write(chunk)


def extract_dataset() -> None:
    corpus_path = SCIFACT_DIR / "corpus.jsonl"
    if corpus_path.exists():
        return
    if SCIFACT_DIR.exists():
        shutil.rmtree(SCIFACT_DIR)
    SCIFACT_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(SCIFACT_DIR)


def _dataset_file(*parts: str) -> Path:
    direct = SCIFACT_DIR.joinpath(*parts)
    if direct.exists():
        return direct
    nested = SCIFACT_DIR / DATASET
    candidate = nested.joinpath(*parts)
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Dataset file not found: {'/'.join(parts)}")


def load_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_scifact() -> Tuple[List[CorpusDoc], Dict[str, str], Dict[str, Dict[str, int]]]:
    corpus_rows = load_jsonl(_dataset_file("corpus.jsonl"))
    query_rows = load_jsonl(_dataset_file("queries.jsonl"))
    qrels_path = _dataset_file("qrels", "test.tsv")

    corpus = [
        CorpusDoc(
            doc_id=row["_id"],
            title=row.get("title", ""),
            text=row.get("text", ""),
        )
        for row in corpus_rows
    ]
    queries = {row["_id"]: row["text"] for row in query_rows}
    qrels: Dict[str, Dict[str, int]] = {}
    with qrels_path.open() as handle:
        header = handle.readline().strip().split("\t")
        if header[:3] != ["query-id", "corpus-id", "score"]:
            raise RuntimeError(f"Unexpected qrels header: {header}")
        for line in handle:
            query_id, corpus_id, score_text = line.rstrip("\n").split("\t")
            qrels.setdefault(query_id, {})[corpus_id] = int(score_text)
    # Canonical BEIR test split: run + score ONLY the queries that have test qrels
    # (SciFact test = ~300). queries.jsonl holds the FULL set (train+test, ~1109);
    # without this filter the run covers train+test and the artifact mislabels the
    # count as test_queries — which reads as train-contamination to a BEIR-literate
    # reviewer. Scoring already iterated qrels (test) only, so the metric is unchanged;
    # this also stops embedding ~800 train queries that never contributed to the score.
    queries = {qid: text for qid, text in queries.items() if qid in qrels}
    return corpus, queries, qrels


EMBED_SUBBATCH = 16  # bounded so a shared GPU with low free memory can't OOM


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed in bounded sub-batches with retry/backoff (the embedding server
    shares a GPU; large single batches OOM → HTTP 500)."""
    out: List[List[float]] = []
    for i in range(0, len(texts), EMBED_SUBBATCH):
        sub = texts[i:i + EMBED_SUBBATCH]
        for attempt in range(5):
            try:
                response = requests.post(
                    EMBEDDING_URL,
                    json={"model": EMBEDDING_MODEL, "input": sub},
                    timeout=180,
                )
                response.raise_for_status()
                data = response.json()["data"]
                data.sort(key=lambda item: item["index"])
                out.extend(item["embedding"] for item in data)
                break
            except Exception as exc:
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
    return out


def class_exists() -> bool:
    response = requests.get(f"{WEAVIATE_URL}/v1/schema/{CLASS_NAME}", timeout=20)
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return False


def delete_class() -> None:
    response = requests.delete(f"{WEAVIATE_URL}/v1/schema/{CLASS_NAME}", timeout=60)
    if response.status_code not in (200, 204, 404):
        raise RuntimeError(f"Failed to delete {CLASS_NAME}: HTTP {response.status_code} {response.text[:200]}")


def create_class() -> None:
    schema = {
        "class": CLASS_NAME,
        "description": "BEIR SciFact public benchmark corpus",
        "vectorizer": "none",
        "properties": [
            {"name": "doc_id", "dataType": ["text"]},
            {"name": "title", "dataType": ["text"]},
            {"name": "text", "dataType": ["text"]},
            {"name": "content", "dataType": ["text"]},
        ],
    }
    response = requests.post(f"{WEAVIATE_URL}/v1/schema", json=schema, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to create {CLASS_NAME}: HTTP {response.status_code} {response.text[:200]}")


def count_objects() -> int:
    query = f"{{ Aggregate {{ {CLASS_NAME} {{ meta {{ count }} }} }} }}"
    response = requests.post(f"{WEAVIATE_URL}/v1/graphql", json={"query": query}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"Aggregate query failed: {json.dumps(errors)[:300]}")
    return payload["data"]["Aggregate"][CLASS_NAME][0]["meta"]["count"]


def ingest_corpus(corpus: List[CorpusDoc], reindex: bool) -> None:
    if reindex and class_exists():
        print(f"Deleting existing class {CLASS_NAME}")
        delete_class()

    if not class_exists():
        print(f"Creating class {CLASS_NAME}")
        create_class()
    else:
        current_count = count_objects()
        if current_count == len(corpus):
            print(f"Reusing existing {CLASS_NAME} index with {current_count} docs")
            return
        print(f"Rebuilding {CLASS_NAME}: found {current_count}, expected {len(corpus)}")
        delete_class()
        create_class()

    print(f"Embedding and ingesting {len(corpus)} SciFact docs into {CLASS_NAME}")
    uploaded = 0
    for start in range(0, len(corpus), INGEST_BATCH_SIZE):
        batch = corpus[start:start + INGEST_BATCH_SIZE]
        vectors = embed_texts([doc.content for doc in batch])
        objects = []
        for doc, vector in zip(batch, vectors):
            object_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"beir-{DATASET}/{doc.doc_id}"))
            objects.append(
                {
                    "id": object_id,
                    "class": CLASS_NAME,
                    "properties": {
                        "doc_id": doc.doc_id,
                        "title": doc.title,
                        "text": doc.text,
                        "content": doc.content,
                    },
                    "vector": vector,
                }
            )
        response = requests.post(
            f"{WEAVIATE_URL}/v1/batch/objects",
            json={"objects": objects},
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        # Weaviate /v1/batch/objects returns a LIST; each item carries result.errors on failure
        errors = []
        if isinstance(payload, list):
            for item in payload:
                res = (item or {}).get("result") or {}
                if res.get("errors"):
                    errors.append(res["errors"])
        elif isinstance(payload, dict) and payload.get("errors"):
            errors.append(payload["errors"])
        if errors:
            raise RuntimeError(f"Batch ingest failed: {json.dumps(errors)[:300]}")
        uploaded += len(batch)
        print(f"  uploaded {uploaded}/{len(corpus)}")


def _graphql_search(query: str) -> List[dict]:
    response = requests.post(f"{WEAVIATE_URL}/v1/graphql", json={"query": query}, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL query failed: {json.dumps(payload['errors'])[:300]}")
    return payload["data"]["Get"][CLASS_NAME]


def dense_search(query_text: str, top_k: int) -> List[str]:
    vector = embed_texts([query_text])[0]
    gql = f"""{{
      Get {{
        {CLASS_NAME}(
          nearVector: {{ vector: {json.dumps(vector)} }}
          limit: {top_k}
        ) {{
          doc_id
          _additional {{ id distance certainty }}
        }}
      }}
    }}"""
    rows = _graphql_search(gql)
    return [row["doc_id"] for row in rows]


def hybrid_search(query_text: str, top_k: int, alpha: float) -> List[str]:
    vector = embed_texts([query_text])[0]
    safe_query = query_text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    gql = f"""{{
      Get {{
        {CLASS_NAME}(
          hybrid: {{
            query: "{safe_query}"
            alpha: {alpha}
            vector: {json.dumps(vector)}
          }}
          limit: {top_k}
        ) {{
          doc_id
          _additional {{ id score }}
        }}
      }}
    }}"""
    rows = _graphql_search(gql)
    return [row["doc_id"] for row in rows]


def recall_at_k(ranked: List[str], relevant: Dict[str, int], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(1 for doc_id in ranked[:k] if relevant.get(doc_id, 0) > 0)
    return hits / len(relevant)


def mrr_at_k(ranked: List[str], relevant: Dict[str, int], k: int) -> float:
    for rank, doc_id in enumerate(ranked[:k], start=1):
        if relevant.get(doc_id, 0) > 0:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: List[str], relevant: Dict[str, int], k: int) -> float:
    dcg = 0.0
    for rank, doc_id in enumerate(ranked[:k], start=1):
        rel = relevant.get(doc_id, 0)
        if rel > 0:
            dcg += (2 ** rel - 1) / math.log2(rank + 1)

    ideal_rels = sorted((score for score in relevant.values() if score > 0), reverse=True)[:k]
    idcg = 0.0
    for rank, rel in enumerate(ideal_rels, start=1):
        idcg += (2 ** rel - 1) / math.log2(rank + 1)
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def evaluate_run(run: Dict[str, List[str]], qrels: Dict[str, Dict[str, int]], k: int) -> Dict[str, float]:
    recalls = []
    ndcgs = []
    mrrs = []
    for query_id, relevant in qrels.items():
        ranked = run.get(query_id, [])
        recalls.append(recall_at_k(ranked, relevant, k))
        ndcgs.append(ndcg_at_k(ranked, relevant, k))
        mrrs.append(mrr_at_k(ranked, relevant, k))
    total = len(qrels)
    return {
        f"Recall@{k}": sum(recalls) / total,
        f"nDCG@{k}": sum(ndcgs) / total,
        f"MRR@{k}": sum(mrrs) / total,
    }


def run_benchmark(queries: Dict[str, str], qrels: Dict[str, Dict[str, int]], top_k: int) -> Dict[str, Dict[str, float]]:
    dense_run: Dict[str, List[str]] = {}
    hybrid_run: Dict[str, List[str]] = {}
    total = len(queries)

    for index, (query_id, query_text) in enumerate(queries.items(), start=1):
        dense_run[query_id] = dense_search(query_text, top_k=top_k)
        hybrid_run[query_id] = hybrid_search(query_text, top_k=top_k, alpha=HYBRID_ALPHA)
        if index % 25 == 0 or index == total:
            print(f"  queried {index}/{total}")

    return {
        "dense": evaluate_run(dense_run, qrels, top_k),
        "hybrid": evaluate_run(hybrid_run, qrels, top_k),
    }


def write_results(metrics: Dict[str, Dict[str, float]], corpus_size: int, query_count: int) -> None:
    payload = {
        "dataset": "BEIR SciFact",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "weaviate_url": WEAVIATE_URL,
            "embedding_url": EMBEDDING_URL,
            "embedding_model": EMBEDDING_MODEL,
            "class_name": CLASS_NAME,
            "top_k": TOP_K,
            "hybrid_alpha": HYBRID_ALPHA,
            "layers_active": {
                "dense_vectors": True,
                "weaviate_bm25": True,
                "isma_query_classifier": False,
                "isma_hmm_rerank": False,
                "isma_phi_layers": False,
            },
        },
        "dataset_stats": {
            "corpus_docs": corpus_size,
            "test_queries": query_count,
        },
        "metrics": metrics,
        "reproduce": "PYTHONPATH=. python3 benchmarks/beir_eval.py",
    }
    with RESULT_PATH.open("w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def print_metrics(metrics: Dict[str, Dict[str, float]]) -> None:
    print("\nSciFact metrics")
    print("mode    nDCG@10   Recall@10   MRR@10")
    for mode in ("dense", "hybrid"):
        row = metrics[mode]
        print(
            f"{mode:<6} {row['nDCG@10']:.4f}    {row['Recall@10']:.4f}      {row['MRR@10']:.4f}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run BEIR SciFact benchmark against Weaviate")
    parser.add_argument("--reindex", action="store_true", help="Force recreation of the BEIR_SciFact Weaviate class")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_dirs()
    check_services()
    download_dataset()
    extract_dataset()
    corpus, queries, qrels = load_scifact()
    ingest_corpus(corpus, reindex=args.reindex)
    metrics = run_benchmark(queries, qrels, top_k=TOP_K)
    write_results(metrics, corpus_size=len(corpus), query_count=len(queries))
    print_metrics(metrics)
    print(f"\nResults written to {RESULT_PATH}")
    print("Reproduce with:")
    print("  PYTHONPATH=. python3 benchmarks/beir_eval.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
