#!/bin/bash
# Start Qwen3-Embedding-8B Server
# Runs a single inference container on the local GPU.
#
# Environment:
#   MODEL_NAME      — HuggingFace model ID (default: Qwen/Qwen3-Embedding-8B)
#   PORT            — host port (default: 8081)
#   MAX_BATCH_SIZE  — max texts per request (default: 512)
#   MAX_SEQ_LEN     — max token length (default: 4096)
#   IMAGE           — Docker image (default: nvcr.io/nvidia/tensorrt-llm/release:spark-single-gpu-dev)

set -e

PORT="${PORT:-8081}"
IMAGE="${IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:spark-single-gpu-dev}"
CONTAINER_NAME="${CONTAINER_NAME:-qwen3-embedding-server}"

# Stop existing container if running
docker stop "$CONTAINER_NAME" 2>/dev/null || true
docker rm "$CONTAINER_NAME" 2>/dev/null || true

echo "Starting Qwen3-Embedding Server on port $PORT..."

docker run -d \
  --name "$CONTAINER_NAME" \
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
  -e USE_COMPILE="${USE_COMPILE:-false}" \
  "$IMAGE" \
  python3 /workspace/server.py

echo "Container started. Waiting for model to load (~70s)..."
echo "Check logs with: docker logs -f $CONTAINER_NAME"
echo "Health check: curl http://localhost:$PORT/health"
