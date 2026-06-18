#!/bin/bash
# Start 2 Qwen3-Embedding-8B instances on this node's GPU.
# Each instance uses ~15GB VRAM — 2 instances = ~30GB.
#
# For multi-node clusters, run this on each node and configure
# an nginx load balancer across all instances.
#
# Environment:
#   IMAGE     — Docker image
#   BASE_PORT — first instance port (default: 8081)

set -e

NUM_INSTANCES=2
BASE_PORT=${BASE_PORT:-8081}
IMAGE="${IMAGE:-nvcr.io/nvidia/tensorrt-llm/release:spark-single-gpu-dev}"

echo "=== Embedding Server Deployment ==="
echo "Host: $(hostname)"
echo "Instances: $NUM_INSTANCES"
echo "Ports: $BASE_PORT-$((BASE_PORT + NUM_INSTANCES - 1))"
echo ""

# Stop and remove existing containers
echo "Cleaning up existing containers..."
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
  NAME="qwen3-embedding-$i"
  docker stop "$NAME" 2>/dev/null || true
  docker rm "$NAME" 2>/dev/null || true
done

# Start instances
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
  PORT=$((BASE_PORT + i))
  NAME="qwen3-embedding-$i"

  echo "Starting $NAME on port $PORT..."

  docker run -d \
    --name "$NAME" \
    --gpus '"device=0"' \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    --memory=32g \
    --memory-swap=32g \
    --restart=unless-stopped \
    -p "$PORT":8080 \
    -v "$(pwd)":/workspace \
    -v "${HF_CACHE:-$HOME/.cache/huggingface}":/root/.cache/huggingface \
    -e MODEL_NAME="${MODEL_NAME:-Qwen/Qwen3-Embedding-8B}" \
    -e MAX_BATCH_SIZE="${MAX_BATCH_SIZE:-256}" \
    -e MAX_SEQ_LEN=4096 \
    -e USE_COMPILE=false \
    -e CUDA_VISIBLE_DEVICES=0 \
    -e INSTANCE_ID=$i \
    "$IMAGE" \
    python3 /workspace/server.py

  echo "  Started $NAME (container ID: $(docker ps -q -f name=$NAME))"
done

echo ""
echo "=== Deployment Complete ==="
echo "Waiting for models to load (~70s)..."
echo ""
echo "Health check commands:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
  echo "  curl http://localhost:$((BASE_PORT + i))/health"
done
echo ""
echo "To verify all instances:"
echo "  docker ps | grep qwen3-embedding"
