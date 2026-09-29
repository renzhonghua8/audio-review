#!/usr/bin/env bash
set -euo pipefail

SERVER_IP="${1:-}"
APP_BASE=/opt/audio-review
DATA_DIR="$APP_BASE/data"
BACKUP_DIR="$APP_BASE/backups"
APP_SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE=audio-review:2.0
CONTAINER=audio-review
WORKERS="${AUDIO_REVIEW_WORKERS:-2}"
MODEL_THREADS="${AUDIO_REVIEW_MODEL_THREADS:-2}"

if [[ $(id -u) -ne 0 ]]; then
  echo '请以 root 执行部署。' >&2
  exit 1
fi
if [[ ! "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo '用法：bash scripts/deploy-docker.sh 服务器实际内网IPv4地址' >&2
  exit 1
fi
if ! ip -o -4 addr show | awk -v requested="$SERVER_IP" '
  { split($4, address, "/"); if (address[1] == requested) found = 1 }
  END { exit !found }'; then
  echo "服务器网卡上没有 $SERVER_IP，请使用实际网卡 IP。" >&2
  exit 1
fi
for value in "$WORKERS" "$MODEL_THREADS"; do
  case "$value" in
    [1-8]) ;;
    *) echo '评测并发数和模型线程数必须在 1–8 之间。' >&2; exit 1 ;;
  esac
done
docker info >/dev/null

exists=false
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  exists=true
  previous_data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")"
  if [[ "$previous_data" != "$DATA_DIR" ]]; then
    echo "旧容器的数据目录为 $previous_data；请按部署说明迁移到 $DATA_DIR 后再执行。" >&2
    exit 1
  fi
fi

# Build before stopping the existing service, so a build failure leaves it running.
docker build -t "$IMAGE" "$APP_SOURCE"
mkdir -p "$DATA_DIR" "$BACKUP_DIR"

if $exists; then
  if [[ $(docker inspect --format '{{.State.Running}}' "$CONTAINER") == true ]]; then
    docker exec "$CONTAINER" python -c '
import json, os, sys
from urllib.request import build_opener, ProxyHandler
port = os.environ.get("AUDIO_REVIEW_PORT", "8001")
with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + port + "/api/reviews", timeout=10) as response:
    rows = json.load(response)["items"]
if any(row["status"] in {"queued", "processing"} for row in rows):
    sys.exit("还有音频正在检测，请等队列完成后重新执行部署。")
'
    docker stop --time 60 "$CONTAINER"
  fi
  backup="$BACKUP_DIR/data-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$backup" -C "$APP_BASE" data
  echo "旧数据已备份：$backup"
  docker rm "$CONTAINER"
fi

chown 10001:10001 "$DATA_DIR"
ORIGINS="http://${SERVER_IP}:8001"
if [[ -n ${AUDIO_REVIEW_EXTRA_ORIGINS:-} ]]; then
  ORIGINS="$ORIGINS,$AUDIO_REVIEW_EXTRA_ORIGINS"
fi
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --init \
  -p "${SERVER_IP}:8001:8001" \
  -e "AUDIO_REVIEW_ALLOWED_ORIGINS=$ORIGINS" \
  -e "AUDIO_REVIEW_WORKERS=$WORKERS" \
  -e "AUDIO_REVIEW_MODEL_THREADS=$MODEL_THREADS" \
  -v "$DATA_DIR:/data:Z" \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  "$IMAGE"

for attempt in {1..30}; do
  if docker exec "$CONTAINER" python -c '
import json, os, sys
from urllib.request import build_opener, ProxyHandler
with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + os.environ["AUDIO_REVIEW_PORT"] + "/api/health", timeout=3) as response:
    result = json.load(response)
sys.exit(0 if result.get("ready") and result.get("evaluator_version") == "2.0" else 1)
' >/dev/null 2>&1; then
    echo "部署完成：http://${SERVER_IP}:8001/"
    echo "音频和评分目录：$DATA_DIR；同时评测文件数：$WORKERS"
    exit 0
  fi
  sleep 2
done
docker logs --tail 100 "$CONTAINER"
echo '容器尚未通过健康检查，请查看上方日志。' >&2
exit 1
