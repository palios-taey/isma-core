"""
ISMA Configuration - Environment Variable Driven

All service endpoints and paths are read from environment variables.
Core external dependencies fail loud when unset so adopters do not
silently connect to the wrong machine.

Loads .env file from repo root if python-dotenv is installed.

Usage:
    from isma.config import WEAVIATE_URL, NEO4J_URI, REDIS_HOST
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    load_dotenv(_env_path, override=False)


def _require_env(name: str, example: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"set {name} - e.g. {example}")

# --- Vector Store (Weaviate) ---
WEAVIATE_URL = _require_env("WEAVIATE_URL", "http://localhost:8080")
WEAVIATE_CLASS = "ISMA_Quantum"

# --- Knowledge Graph (Neo4j) ---
# Optional: only the graph-enrichment features use Neo4j; core search runs without it.
# Defaults to localhost so importing this module never hard-fails when Neo4j is unused.
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")

# --- Cache & HMM Index (Redis) ---
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))

# --- Embedding Server ---
EMBEDDING_URL = _require_env("EMBEDDING_URL", "http://localhost:8089/v1/embeddings")

# --- Reranker ---
RERANKER_URL = os.environ.get("RERANKER_URL", "http://localhost:8085")
RERANKER_ENGINE = os.environ.get("RERANKER_ENGINE", "qwen3")

# --- Query API ---
ISMA_QUERY_API = os.environ.get("ISMA_QUERY_API", "http://localhost:8095")

# --- Data Directories ---
# Default to a user-writable location so a non-root adopter does not hit PermissionError;
# override ISMA_STATE_DIR for a system-wide deployment.
ISMA_STATE_DIR = os.environ.get(
    "ISMA_STATE_DIR", os.path.expanduser("~/.local/share/isma")
)
ISMA_DATA_DIR = os.environ.get("ISMA_DATA_DIR", os.path.expanduser("~/.local/share/isma/data"))
ISMA_CANONICAL_MAPPING_PATH = os.environ.get(
    "ISMA_CANONICAL_MAPPING_PATH",
    str(Path(ISMA_STATE_DIR) / "canonical_mapping.json"),
)
ISMA_TEMPORAL_LOG_DIR = os.environ.get(
    "ISMA_TEMPORAL_LOG_DIR",
    "/tmp/isma",
)
ISMA_TEMPORAL_DOLT_DIR = os.environ.get(
    "ISMA_TEMPORAL_DOLT_DIR",
    "/tmp/isma-dolt",
)
ISMA_THEME_INDEX_PATH = os.environ.get(
    "ISMA_THEME_INDEX_PATH",
    str(Path(ISMA_STATE_DIR) / "theme_search_index.json"),
)
ISMA_HMM_PKG_STATE_PATH = os.environ.get(
    "ISMA_HMM_PKG_STATE_PATH",
    str(Path(ISMA_STATE_DIR) / "hmm_pkg_state.json"),
)
ISMA_HMM_PKG_DIR = os.environ.get("ISMA_HMM_PKG_DIR", "/tmp/hmm_packages")
ISMA_BENCHMARK_OUTPUT_DIR = os.environ.get(
    "ISMA_BENCHMARK_OUTPUT_DIR",
    str(Path(ISMA_STATE_DIR) / "benchmarks"),
)
ISMA_MANIFEST_DIR = os.environ.get("ISMA_MANIFEST_DIR", ISMA_STATE_DIR)

# --- Nightly Ingest (optional) ---
NIGHTLY_SYNC_HOST = os.environ.get("NIGHTLY_SYNC_HOST", "")
NIGHTLY_SYNC_CLAUDE_PROJECTS = os.environ.get("NIGHTLY_SYNC_CLAUDE_PROJECTS", "")
NIGHTLY_SYNC_HOME = os.environ.get("NIGHTLY_SYNC_HOME", "")

# Backward-compatible aliases for older scripts that still import the prior names.
NIGHTLY_MAC_HOST = NIGHTLY_SYNC_HOST
NIGHTLY_MAC_CLAUDE_PROJECTS = NIGHTLY_SYNC_CLAUDE_PROJECTS
NIGHTLY_MAC_HOME = NIGHTLY_SYNC_HOME
