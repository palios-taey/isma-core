#!/bin/bash
# Start multiple Qwen3-Embedding-8B instances sequentially.
# Waits for each to be healthy before starting the next (avoids CUDA memory contention).
#
# Environment:
#   NUM_INSTANCES — number of instances (default: 4)
#   BASE_PORT     — first instance port (default: 8081)
#   IMAGE         — Docker image

set -e

NUM_INSTANCES=${NUM_INSTANCES:-4}
BASE_PORT=${BASE_PORT:-8081}
IMAGE="${IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:spark-single-gpu-dev}"

echo "Starting $NUM_INSTANCES embedding server instances (sequentially)..."

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
  PORT=$((BASE_PORT + i))
  NAME="qwen3-embedding-$i"

  docker stop "$NAME" 2>/dev/null || true
  docker rm "$NAME" 2>/dev/null || true

  echo "Starting instance $i on port $PORT..."

  docker run -d \
    --name "$NAME" \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    -p "$PORT":8080 \
    -v "$(pwd)":/workspace \
    -v "${HF_CACHE:-$HOME/.cache/huggingface}":/root/.cache/huggingface \
    -e MODEL_NAME="${MODEL_NAME:-Qwen/Qwen3-Embedding-8B}" \
    -e MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-512}" \
    -e MAX_SEQ_LEN="${MAX_SEQ_LEN:-4096}" \
    -e USE_COMPILE=false \
    "$IMAGE" \
    python3 /workspace/server.py

  # Wait for this instance to become healthy
  echo "  Waiting for instance $i to be healthy..."
  for attempt in $(seq 1 60); do
    if curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
      echo "  Instance $i is healthy!"
      break
    fi
    sleep 3
  done

  if ! curl -s "http://localhost:$PORT/health" > /dev/null 2>&1; then
    echo "  ERROR: Instance $i failed to start!"
    docker logs "$NAME" --tail 20
    exit 1
  fi
done

echo ""
echo "All $NUM_INSTANCES instances started on ports $BASE_PORT-$((BASE_PORT + NUM_INSTANCES - 1))"
