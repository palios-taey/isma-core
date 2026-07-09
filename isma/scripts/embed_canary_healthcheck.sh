#!/usr/bin/env bash
# Fail-loud canary for the isma-embedding server (:8089). /health does NOT exercise the model,
# so a wedged forward path (e.g. corrupted torch.compile cudagraph) returns 200 on /health while
# every /v1/embeddings returns 500 — silent staleness. This canary embeds a real string and
# restarts the --user unit if the EMBED PATH (not just /health) is broken.
set -u
URL="http://localhost:8089/v1/embeddings"
MODEL="Qwen/Qwen3-Embedding-8B"
LOG="${ISMA_CANARY_LOG:-/tmp/embed_canary.log}"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
resp=$(curl -s -m20 -o /tmp/embed_canary_resp.json -w "%{http_code}" -X POST "$URL" \
  -H 'Content-Type: application/json' \
  -d "{\"input\":[\"embed canary healthcheck\"],\"model\":\"$MODEL\"}" 2>/dev/null)
dim=$(python3 -c "import json;print(len(json.load(open('/tmp/embed_canary_resp.json'))['data'][0]['embedding']))" 2>/dev/null)
if [ "$resp" = "200" ] && [ "${dim:-0}" -ge 1024 ]; then
  exit 0   # healthy — embed path works
fi
echo "$(ts) CANARY FAIL: http=$resp dim=${dim:-none} — embed path dead, restarting isma-embedding.service" >> "$LOG"
systemctl --user restart isma-embedding.service >> "$LOG" 2>&1
echo "$(ts) restart issued" >> "$LOG"
exit 1
