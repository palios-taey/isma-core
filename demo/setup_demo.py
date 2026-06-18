#!/usr/bin/env python3
"""
ISMA Demo Setup — bring your own corpus

Ingests a local corpus of Markdown documents into a Weaviate instance so you can
query across them. Drop your own .md files into demo/corpus/ (strategic docs,
reference documentation, exported conversation logs — whatever you want to blend
into one queryable space), then run this script.

Requirements:
    pip install weaviate-client requests
    Weaviate running at localhost:8080 (see docker-compose.yml)
    Embedding server running at localhost:8089 (or set EMBEDDING_URL)

Usage:
    python3 demo/setup_demo.py           # Ingest all .md files in demo/corpus/
    python3 demo/setup_demo.py --query "your question here"
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

import requests

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8080")
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "http://localhost:8089/v1/embeddings")
CORPUS_DIR = os.path.join(os.path.dirname(__file__), "corpus")
CLASS_NAME = "ISMA_Demo"
CHUNK_SIZE = 512  # tokens approx (chars / 4)


def chunk_text(text: str, source: str, chunk_chars: int = 2000) -> list[dict]:
    """Split text into overlapping chunks."""
    paragraphs = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
    chunks = []
    current = []
    current_len = 0
    chunk_idx = 0

    for para in paragraphs:
        if current_len + len(para) > chunk_chars and current:
            chunks.append({
                "content": "\n\n".join(current),
                "source": source,
                "chunk_idx": chunk_idx,
                "content_hash": hashlib.sha256("\n\n".join(current).encode()).hexdigest()[:16],
            })
            chunk_idx += 1
            # Overlap: keep last paragraph
            current = [current[-1], para]
            current_len = len(current[-2]) + len(para)
        else:
            current.append(para)
            current_len += len(para)

    if current:
        chunks.append({
            "content": "\n\n".join(current),
            "source": source,
            "chunk_idx": chunk_idx,
            "content_hash": hashlib.sha256("\n\n".join(current).encode()).hexdigest()[:16],
        })

    return chunks


def embed(texts: list[str]) -> list[list[float]]:
    """Call embedding server."""
    resp = requests.post(EMBEDDING_URL, json={"input": texts, "model": "Qwen/Qwen3-Embedding-8B"}, timeout=60)
    resp.raise_for_status()
    data = resp.json()["data"]
    data.sort(key=lambda item: item["index"])
    return [item["embedding"] for item in data]


def create_schema():
    """Create Weaviate class for demo."""
    schema = {
        "class": CLASS_NAME,
        "description": "Foundational demo documents",
        "vectorizer": "none",
        "properties": [
            {"name": "content", "dataType": ["text"]},
            {"name": "source", "dataType": ["text"]},
            {"name": "chunk_idx", "dataType": ["int"]},
            {"name": "content_hash", "dataType": ["text"]},
        ],
    }

    # Delete if exists
    resp = requests.delete(f"{WEAVIATE_URL}/v1/schema/{CLASS_NAME}")

    resp = requests.post(f"{WEAVIATE_URL}/v1/schema", json=schema)
    if resp.status_code == 200:
        print(f"Created schema: {CLASS_NAME}")
    else:
        print(f"Schema error: {resp.status_code} {resp.text}")
        sys.exit(1)


def ingest():
    """Read corpus files, chunk, embed, upload to Weaviate."""
    files = sorted([f for f in os.listdir(CORPUS_DIR) if f.endswith(".md")])
    if not files:
        print(f"No .md files found in {CORPUS_DIR}")
        sys.exit(1)

    all_chunks = []
    for fname in files:
        path = os.path.join(CORPUS_DIR, fname)
        with open(path) as f:
            text = f.read()
        chunks = chunk_text(text, fname.replace(".md", ""))
        print(f"  {fname}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    print(f"\nTotal chunks: {len(all_chunks)}")

    # Batch embed + upload
    batch_size = 16
    uploaded = 0
    for i in range(0, len(all_chunks), batch_size):
        batch = all_chunks[i:i + batch_size]
        texts = [c["content"] for c in batch]

        try:
            vectors = embed(texts)
        except Exception as e:
            print(f"Embedding error at batch {i}: {e}")
            sys.exit(1)

        objects = []
        for chunk, vector in zip(batch, vectors):
            objects.append({
                "class": CLASS_NAME,
                "properties": {
                    "content": chunk["content"],
                    "source": chunk["source"],
                    "chunk_idx": chunk["chunk_idx"],
                    "content_hash": chunk["content_hash"],
                },
                "vector": vector,
            })

        resp = requests.post(
            f"{WEAVIATE_URL}/v1/batch/objects",
            json={"objects": objects},
        )
        if resp.status_code != 200:
            print(f"Upload error: {resp.status_code} {resp.text}")
            sys.exit(1)

        uploaded += len(batch)
        print(f"  Uploaded {uploaded}/{len(all_chunks)}")

    print(f"\nDone. {uploaded} chunks indexed in Weaviate class '{CLASS_NAME}'.")


def query(q: str, top_k: int = 5):
    """Embed query and search Weaviate."""
    try:
        vectors = embed([q])
    except Exception as e:
        print(f"Embedding error: {e}")
        sys.exit(1)

    resp = requests.post(
        f"{WEAVIATE_URL}/v1/graphql",
        json={
            "query": f"""
            {{
              Get {{
                {CLASS_NAME}(
                  nearVector: {{ vector: {json.dumps(vectors[0])} }}
                  limit: {top_k}
                ) {{
                  content
                  source
                  chunk_idx
                  _additional {{ distance }}
                }}
              }}
            }}
            """
        },
    )
    resp.raise_for_status()
    results = resp.json()["data"]["Get"][CLASS_NAME]

    print(f"\nQuery: {q!r}\n")
    for i, r in enumerate(results, 1):
        dist = r["_additional"]["distance"]
        print(f"[{i}] {r['source']} (chunk {r['chunk_idx']}, dist={dist:.3f})")
        print(f"     {r['content'][:300].replace(chr(10), ' ')}...")
        print()


def main():
    parser = argparse.ArgumentParser(description="ISMA Demo Setup")
    parser.add_argument("--query", "-q", help="Search query (skips ingest)")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    # Check Weaviate
    try:
        resp = requests.get(f"{WEAVIATE_URL}/v1/meta", timeout=5)
        print(f"Weaviate {resp.json().get('version')} at {WEAVIATE_URL}")
    except Exception as e:
        print(f"Cannot reach Weaviate at {WEAVIATE_URL}: {e}")
        print("Start it with: docker compose up -d")
        sys.exit(1)

    if args.query:
        query(args.query, args.top_k)
    else:
        create_schema()
        ingest()
        print("\nTry querying:")
        print("  python3 demo/setup_demo.py --query 'what is the architecture?'")
        print("  python3 demo/setup_demo.py --query 'how do the systems connect?'")


if __name__ == "__main__":
    main()
