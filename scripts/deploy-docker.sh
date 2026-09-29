#!/usr/bin/env bash
set -euo pipefail

VERSION=2.0.9
IMAGE="audio-review:$VERSION"
CONTAINER=audio-review
MANAGED_LABEL=io.github.renzhonghua8.audio-review.managed
APP_BASE=/opt/audio-review
DATA_DIR="$APP_BASE/data"
BACKUP_DIR="$APP_BASE/backups"
IMAGE_DIR="$APP_BASE/images"
WORKERS="${AUDIO_REVIEW_WORKERS:-2}"
MODEL_THREADS="${AUDIO_REVIEW_MODEL_THREADS:-1}"
PLAYBACK_CACHE_MB="${AUDIO_REVIEW_PLAYBACK_CACHE_MB:-1024}"
MEMORY_REQUEST="${AUDIO_REVIEW_MEMORY_MB:-auto}"
HOST_RESERVE_MB=512
MEMORY_MB=''
SERVER_IP=''
UPGRADE=false
STOP_FIRST=false
INTERRUPT_TASKS=false

fail() { echo "$*" >&2; exit 1; }
for argument in "$@"; do
  case "$argument" in
    --upgrade) UPGRADE=true ;;
    --stop-first) STOP_FIRST=true ;;
    --interrupt-tasks) INTERRUPT_TASKS=true ;;
    --*) fail '用法：bash scripts/deploy-docker.sh [实际网卡IPv4] [--upgrade [--stop-first] [--interrupt-tasks]]' ;;
    *) [[ -z "$SERVER_IP" ]] || fail '用法：bash scripts/deploy-docker.sh [实际网卡IPv4] [--upgrade [--stop-first] [--interrupt-tasks]]'
       SERVER_IP="$argument" ;;
  esac
done
if $STOP_FIRST && ! $UPGRADE; then fail '--stop-first 必须与 --upgrade 联用。'; fi
if $INTERRUPT_TASKS && ! $UPGRADE; then fail '--interrupt-tasks 必须与 --upgrade 联用。'; fi
[[ $(id -u) -eq 0 ]] || fail '请以 root 执行部署。'
for required_tool in ip ss docker awk df curl sha256sum tar; do
  command -v "$required_tool" >/dev/null || fail "缺少 ${required_tool}；请先安装该工具，本脚本不会修改系统软件。"
done
for value in "$WORKERS" "$MODEL_THREADS"; do
  case "$value" in
    [1-8]) ;;
    *) fail '评测并发数和模型线程数必须在 1–8 之间。' ;;
  esac
done
case "$MEMORY_REQUEST" in
  auto|768|1024) ;;
  *) fail 'AUDIO_REVIEW_MEMORY_MB 仅支持 auto、768 或 1024（MiB）。' ;;
esac
[[ "$PLAYBACK_CACHE_MB" =~ ^[1-9][0-9]{2,4}$ ]] && [[ "$PLAYBACK_CACHE_MB" -ge 512 && "$PLAYBACK_CACHE_MB" -le 16384 ]] ||
  fail 'AUDIO_REVIEW_PLAYBACK_CACHE_MB 必须是 512 到 16384 的整数（MiB）。'

read_available_memory() {
  local memory_info
  # Only use MemFree when the kernel does not expose MemAvailable; cache totals
  # alone cannot establish how much memory can safely be reclaimed.
  memory_info="$(awk '
    $1 == "MemAvailable:" { available=$2; has_available=1; available_valid=($2 ~ /^[0-9]+$/ && $3 == "kB") }
    $1 == "MemFree:" { free_value=$2; free_valid=($2 ~ /^[0-9]+$/ && $3 == "kB") }
    END {
      if (has_available) {
        if (!available_valid) exit 1
        print "MemAvailable", available
      } else if (free_valid) {
        print "MemFree", free_value
      } else exit 1
    }' /proc/meminfo 2>/dev/null)" || fail '无法可靠读取可用内存，部署已停止；请检查 /proc/meminfo。'
  read -r MEMORY_SOURCE AVAILABLE_MEMORY_KB <<< "$memory_info"
  [[ "$AVAILABLE_MEMORY_KB" =~ ^[0-9]+$ ]] || fail '可用内存数据无效，部署已停止。'
  AVAILABLE_MEMORY_MB=$((AVAILABLE_MEMORY_KB / 1024))
}

require_memory_budget() {
  local required_mb=$((MEMORY_MB + HOST_RESERVE_MB))
  [[ "$AVAILABLE_MEMORY_KB" -ge $((required_mb * 1024)) ]] ||
    fail "当前可用内存 ${AVAILABLE_MEMORY_MB} MiB（${MEMORY_SOURCE}）；声检上限 ${MEMORY_MB} MiB，加宿主余量 ${HOST_RESERVE_MB} MiB，共需 ${required_mb} MiB。为保护原服务，部署已停止。"
}

select_memory_budget() {
  read_available_memory
  if [[ "$MEMORY_REQUEST" == auto ]]; then
    if [[ "$AVAILABLE_MEMORY_KB" -ge $(((1024 + HOST_RESERVE_MB) * 1024)) ]]; then
      MEMORY_MB=1024
    else
      MEMORY_MB=768
    fi
  else
    MEMORY_MB="$MEMORY_REQUEST"
  fi
  if ! $STOP_FIRST; then require_memory_budget; fi
  if [[ "$MEMORY_MB" -lt 1024 && ( "$WORKERS" -gt 2 || "$MODEL_THREADS" != 1 ) ]]; then
    fail '768 MiB 模式最多同时评测 2 条，且需要 AUDIO_REVIEW_MODEL_THREADS=1；请使用默认线程数。'
  fi
  echo "当前可用内存 ${AVAILABLE_MEMORY_MB} MiB（${MEMORY_SOURCE}）；声检上限 ${MEMORY_MB} MiB；宿主余量要求 ${HOST_RESERVE_MB} MiB。"
}

if [[ -z "$SERVER_IP" ]]; then
  # Query routing without sending network traffic.
  SERVER_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '
    { for (i=1; i<NF; i++) if ($i == "src") { print $(i+1); exit } }' || true)"
  if [[ -z "$SERVER_IP" ]]; then
    candidates="$(ip -o -4 addr show up scope global | awk '
      $2 !~ /^(docker[0-9]*|br-|veth)/ { split($4, a, "/"); if (!seen[a[1]]++) print a[1] }')"
    count="$(printf '%s\n' "$candidates" | awk 'NF { n++ } END { print n+0 }')"
    [[ "$count" == 1 ]] || fail "无法唯一识别网卡 IP，请执行 ip -4 addr 后传入实际地址。候选：${candidates:-无}"
    SERVER_IP="$candidates"
  fi
fi
[[ "$SERVER_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || fail 'IP 格式错误，请传入服务器实际网卡上的 IPv4。'
if ! ip -o -4 addr show up scope global | awk -v requested="$SERVER_IP" '
  { split($4, a, "/"); if (a[1] == requested) found=1 } END { exit !found }'; then
  fail "服务器网卡上没有 ${SERVER_IP}。可不传 IP，让脚本自动识别。"
fi
echo "使用服务器网卡 IP：${SERVER_IP}；端口：8001"

docker info >/dev/null 2>&1 || fail 'Docker 当前不可用。请先检查原服务状态；本脚本不会启动或重启 Docker。'
capabilities="$(docker info --format '{{.MemoryLimit}} {{.SwapLimit}} {{.CPUCfsQuota}}' 2>/dev/null)" || fail '无法确认 Docker 资源限制能力，部署已停止。'
[[ "$capabilities" == 'true true true' ]] || fail '宿主不支持完整 CPU/内存/交换空间限制，部署已停止；不会修改原服务或内核配置。'

exists=false
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  exists=true
  $UPGRADE || fail '已存在 audio-review 容器，首次部署不会替换它。升级已确认的声检实例时使用 --upgrade。'
  managed="$(docker inspect --format "{{index .Config.Labels \"$MANAGED_LABEL\"}}" "$CONTAINER")"
  [[ "$managed" == true ]] || fail '同名容器没有声检管理标记，拒绝停止或替换。'
  previous_data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER")"
  [[ "$previous_data" == "$DATA_DIR" ]] || fail "旧容器数据目录为 ${previous_data}，拒绝自动修改。"
fi
if $STOP_FIRST && ! $exists; then fail '--stop-first 只适用于已确认归属的现有声检容器。'; fi
if $INTERRUPT_TASKS && ! $exists; then fail '--interrupt-tasks 只适用于已确认归属的现有声检容器。'; fi

check_port() {
  local running bindings name owned_bindings='' listener listeners
  for running in $(docker ps -q); do
    bindings="$(docker inspect --format '{{range .NetworkSettings.Ports}}{{range .}}{{if eq .HostPort "8001"}}{{.HostIp}}:{{.HostPort}} {{end}}{{end}}{{end}}' "$running")"
    [[ -n "$bindings" ]] || continue
    name="$(docker inspect --format '{{.Name}}' "$running")"
    if $exists && $UPGRADE && [[ "$name" == "/$CONTAINER" ]]; then
      owned_bindings="$bindings"
    else
      fail "8001 端口已被容器 ${name} 占用，部署已停止，该容器保持运行。"
    fi
  done
  listeners="$(ss -ltn | awk '$4 ~ /:8001$/ { print $4 }')"
  for listener in $listeners; do
    case " $owned_bindings " in
      *" $listener "*) ;;
      *) fail "8001 端口已有服务监听（${listener}），部署已停止，原服务保持运行。" ;;
    esac
  done
}
check_port

# A dedicated directory must not be shared with any other container.
for other_container in $(docker ps -aq); do
  other_name="$(docker inspect --format '{{.Name}}' "$other_container")"
  if $exists && [[ "$other_name" == "/$CONTAINER" ]]; then continue; fi
  mount_sources="$(docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' "$other_container")"
  while IFS= read -r mount_source; do
    [[ -n "$mount_source" ]] || continue
    mount_source="${mount_source%/}"
    if [[ -z "$mount_source" || "$DATA_DIR" == "$mount_source" || "$DATA_DIR" == "$mount_source/"* || "$mount_source" == "$DATA_DIR/"* ]]; then
      fail "声检数据目录与容器 ${other_name} 的挂载重叠，拒绝修改，该容器保持原状。"
    fi
  done <<< "$mount_sources"
done

[[ ! -L "$APP_BASE" && ! -L "$DATA_DIR" && ! -L "$IMAGE_DIR" && ! -L "$BACKUP_DIR" ]] || fail '声检目录包含符号链接，拒绝修改其他位置的数据。'
if ! $exists && [[ -d "$DATA_DIR" ]] && [[ -n "$(ls -A "$DATA_DIR")" ]]; then
  fail '数据目录已有内容且没有可确认的声检容器，拒绝自动接管；原数据保留。'
fi
disk_path="$APP_BASE"
[[ -d "$disk_path" ]] || disk_path=/opt
docker_disk_path="$(docker info --format '{{.DockerRootDir}}')"
[[ -d "$docker_disk_path" ]] || fail '无法确认 Docker 镜像所在磁盘，部署已停止。'
for checked_disk in "$disk_path" "$docker_disk_path"; do
  available_kb="$(df -Pk "$checked_disk" | awk 'NR==2 {print $4}')"
  [[ "$available_kb" =~ ^[0-9]+$ && "$available_kb" -ge 5242880 ]] || fail "${checked_disk} 所在磁盘可用空间不足 5 GB，部署已停止。"
done
select_memory_budget
[[ $(uname -m) == x86_64 ]] || fail '此发布镜像仅支持 Linux x86_64，部署已停止。'

backup_container=''
previous_id=''
previous_running=false
previous_stopped=false
restore_pending=false
new_container=''
download_dir=''
if $exists; then
  previous_id="$(docker inspect --format '{{.Id}}' "$CONTAINER")"
  [[ -n "$previous_id" ]] || fail '无法记录原声检容器 ID，部署已停止。'
  previous_running="$(docker inspect --format '{{.State.Running}}' "$previous_id")"
  [[ "$previous_running" == true || "$previous_running" == false ]] || fail '无法确认原声检运行状态，部署已停止。'
fi

restore_on_failure() {
  local status=$? recovery_label recovery_image recovery_data recovery_id previous_name recovered=true
  trap - EXIT HUP INT TERM
  if [[ "$status" -ne 0 ]] && $restore_pending; then
    if [[ -n "$new_container" ]]; then
      docker rm -f "$new_container" >/dev/null 2>&1 || recovered=false
    elif [[ -n "$backup_container" ]] && docker container inspect "$CONTAINER" >/dev/null 2>&1; then
      recovery_id="$(docker inspect --format '{{.Id}}' "$CONTAINER" 2>/dev/null)" || recovery_id=''
      recovery_label="$(docker inspect --format "{{index .Config.Labels \"$MANAGED_LABEL\"}}" "$CONTAINER" 2>/dev/null)" || recovery_label=''
      recovery_image="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)" || recovery_image=''
      recovery_data="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$CONTAINER" 2>/dev/null)" || recovery_data=''
      if [[ -n "$recovery_id" && "$recovery_id" != "$previous_id" && "$recovery_label" == true && "$recovery_image" == "$IMAGE" && "$recovery_data" == "$DATA_DIR" ]]; then
        docker rm -f "$recovery_id" >/dev/null 2>&1 || recovered=false
      fi
    fi
    if [[ -n "$backup_container" ]]; then
      previous_name="$(docker inspect --format '{{.Name}}' "$previous_id" 2>/dev/null)" || previous_name=''
      if [[ "$previous_name" != "/$CONTAINER" ]]; then
        docker rename "$previous_id" "$CONTAINER" >/dev/null 2>&1 || recovered=false
      fi
    fi
    if $previous_running; then
      docker start "$previous_id" >/dev/null 2>&1 || recovered=false
    fi
    if $recovered; then
      echo "升级失败，已恢复原声检容器（${previous_id}）；其他服务保持原状。" >&2
    else
      echo "升级失败，原声检自动恢复未完成；原容器 ID 为 ${previous_id}，请检查 docker ps 和日志。" >&2
    fi
  fi
  [[ -z "$download_dir" ]] || rm -rf "$download_dir"
  exit "$status"
}
trap restore_on_failure EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

check_previous_queue() {
  $INTERRUPT_TASKS && return 0
  if $previous_running && ! $previous_stopped; then
    docker exec "$previous_id" python -c '
import json, os, sys
from urllib.request import build_opener, ProxyHandler
with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + os.environ.get("AUDIO_REVIEW_PORT", "8001") + "/api/reviews?compact=true", timeout=10) as response:
    rows = json.load(response)["items"]
if any(row["status"] in {"queued", "processing", "pausing"} for row in rows):
    sys.exit("还有音频正在检测或暂停，请等队列完成或任务完全暂停后再升级。")
'
  fi
}

stop_previous() {
  $previous_stopped && return
  [[ "$(docker inspect --format '{{.Id}}' "$CONTAINER")" == "$previous_id" ]] || fail '原声检容器已改变，拒绝继续升级。'
  [[ "$(docker inspect --format "{{index .Config.Labels \"$MANAGED_LABEL\"}}" "$previous_id")" == true ]] || fail '原声检管理标记已改变，拒绝停止。'
  [[ "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$previous_id")" == "$DATA_DIR" ]] || fail '原声检数据挂载已改变，拒绝停止。'
  check_previous_queue
  if $previous_running; then
    # Arm rollback before stopping: even a failed stop may have stopped the container.
    restore_pending=true
    echo "记录原声检容器 ID：${previous_id}；失败时按原容器配置恢复。"
    if $INTERRUPT_TASKS; then
      echo '已请求中断声检任务后升级；只停止原声检，最多等待 30 秒正常退出。未完成任务可能需要重新检测。'
      docker stop --time 30 "$previous_id"
    elif $STOP_FIRST; then
      echo '先关闭空闲的原声检以释放内存；等待正常退出，不强制结束检测。'
      docker stop --time -1 "$previous_id"
    else
      docker stop --time 60 "$previous_id"
    fi
    previous_stopped=true
  fi
}

if $STOP_FIRST; then
  # Port, ownership, mount, disk and thread checks above must all pass before stopping.
  check_previous_queue
  if [[ "$AVAILABLE_MEMORY_KB" -lt $(((MEMORY_MB + HOST_RESERVE_MB) * 1024)) ]]; then
    stop_previous
    read_available_memory
    require_memory_budget
    echo "关闭原声检后可用内存 ${AVAILABLE_MEMORY_MB} MiB；下载与启动预算检查通过。"
  fi
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  mkdir -p "$IMAGE_DIR"
  archive=audio-review-linux-amd64.tar.gz
  release="https://github.com/renzhonghua8/audio-review/releases/download/v$VERSION"
  echo '下载 GitHub 已构建的镜像（限速 2 MB/s），不会在服务器构建。'
  download_dir="$(mktemp -d "$IMAGE_DIR/.download-XXXXXX")"
  if ! (
    curl --fail --location --retry 3 --connect-timeout 15 --limit-rate 2M "$release/$archive" -o "$download_dir/$archive" || exit 1
    curl --fail --location --retry 3 --connect-timeout 15 --limit-rate 2M "$release/$archive.sha256" -o "$download_dir/$archive.sha256" || exit 1
    cd "$download_dir" || exit 1
    sha256sum --check "$archive.sha256" || exit 1
  ); then
    fail '镜像下载或校验失败，部署已停止。'
  fi
  if $STOP_FIRST; then
    stop_previous
    read_available_memory
    require_memory_budget
  fi
  # Image import also uses host memory; stop-first checks the full budget before import.
  docker load --input "$download_dir/$archive" || fail '镜像加载失败，部署已停止。'
  rm -rf "$download_dir"
  download_dir=''
fi
image_managed="$(docker image inspect --format "{{index .Config.Labels \"$MANAGED_LABEL\"}}" "$IMAGE")"
[[ "$image_managed" == true ]] || fail '同名镜像没有声检管理标记，拒绝启动。'
if $STOP_FIRST; then stop_previous; fi

# Importing an image takes time and host memory. Preserve the selected budget,
# and recheck before changing data or stopping an owned previous instance.
read_available_memory
require_memory_budget
echo "启动前复查：当前可用内存 ${AVAILABLE_MEMORY_MB} MiB；资源预算检查通过。"

ORIGINS="http://${SERVER_IP}:8001"
[[ -z ${AUDIO_REVIEW_EXTRA_ORIGINS:-} ]] || ORIGINS="$ORIGINS,$AUDIO_REVIEW_EXTRA_ORIGINS"

mkdir -p "$DATA_DIR" "$BACKUP_DIR"
if $exists; then
  stop_previous
  stamp="$(date +%Y%m%d-%H%M%S)"
  if ! tar -czf "$BACKUP_DIR/data-$stamp.tar.gz" -C "$APP_BASE" data; then
    fail '声检数据备份失败，原声检容器保留。'
  fi
  backup_container="$CONTAINER-backup-$stamp"
  restore_pending=true
  if ! docker rename "$previous_id" "$backup_container"; then
    fail '无法为旧声检容器创建备份名称，已尝试恢复原声检。'
  fi
fi

check_port
if $STOP_FIRST; then
  read_available_memory
  require_memory_budget
fi
chown 10001:10001 "$DATA_DIR"
new_container="$(docker run -d \
  --name "$CONTAINER" \
  --label "$MANAGED_LABEL=true" \
  --restart unless-stopped --init \
  --cpus 0.5 --cpu-shares 128 \
  --memory "${MEMORY_MB}m" --memory-swap "${MEMORY_MB}m" --pids-limit 256 \
  -p "${SERVER_IP}:8001:8001" \
  -e "AUDIO_REVIEW_ALLOWED_ORIGINS=$ORIGINS" \
  -e "AUDIO_REVIEW_WORKERS=$WORKERS" \
  -e "AUDIO_REVIEW_MODEL_THREADS=$MODEL_THREADS" \
  -e "AUDIO_REVIEW_PLAYBACK_CACHE_MB=$PLAYBACK_CACHE_MB" \
  -v "$DATA_DIR:/data:Z" \
  --log-opt max-size=10m --log-opt max-file=3 \
  "$IMAGE")"

for attempt in {1..30}; do
  if docker exec "$CONTAINER" python -c '
import json, os, sys
from urllib.request import build_opener, ProxyHandler
with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + os.environ["AUDIO_REVIEW_PORT"] + "/api/health", timeout=3) as response:
    result = json.load(response)
sys.exit(0 if result.get("ready") and result.get("evaluator_version") == "2.0" else 1)
' >/dev/null 2>&1; then
    [[ -z "$backup_container" ]] || docker rm "$backup_container" >/dev/null
    backup_container=''
    restore_pending=false
    echo "部署完成：http://${SERVER_IP}:8001/"
    echo "资源上限：0.5 核 CPU、${MEMORY_MB} MiB 内存；不使用交换空间。"
    echo "音频和评分：${DATA_DIR}；同时评测文件数：${WORKERS}"
    echo "回听缓存：${PLAYBACK_CACHE_MB} MiB；已保存副本保留，满额后停止生成新副本。"
    exit 0
  fi
  sleep 2
done
docker logs --tail 100 "$CONTAINER"
fail '声检容器未通过健康检查，请查看上方日志。'
