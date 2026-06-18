#!/bin/bash
# start-local.sh - bare-metal launch wrapper for the local embedding server.
#
# The Docker-based start.sh assumes the TensorRT-LLM container; production on
# This wrapper codifies the launch environment so future restarts cannot drift
# back into allocator settings that trigger avoidable OOMs.
#
# PYTORCH_ALLOC_CONF=expandable_segments:True is REQUIRED — without it, the
# CUDA allocator fragments under sustained 4-concurrent load (Semaphore(4) +
# no empty_cache as of commit e4384f3) and OOMs after multi-day uptime.
# Verified on 2026-05-26: 24 days uptime → 23,622/24,564 MiB VRAM,
# OOM on 788 MiB embed allocation. Restart with this flag clears it.
#
# Run:  bash ./start-local.sh
# Log:  /tmp/embedding_server.log (appended)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True}"
export USE_COMPILE="${USE_COMPILE:-false}"
export MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-256}"
export MAX_SEQ_LEN="${MAX_SEQ_LEN:-4096}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-8089}"

PYBIN="${PYBIN:-python3}"
LOG="${LOG:-/tmp/embedding_server.log}"

echo "[$(date -Iseconds)] start-local.sh launching server.py" \
     "PYTORCH_ALLOC_CONF=$PYTORCH_ALLOC_CONF USE_COMPILE=$USE_COMPILE" >> "$LOG"

exec "$PYBIN" server.py >> "$LOG" 2>&1
