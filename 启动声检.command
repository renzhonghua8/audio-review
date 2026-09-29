#!/bin/zsh
set -eu
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ "$(basename "$(dirname "$APP_DIR")")" == "outputs" ]]; then
  TASK_WORK="$(dirname "$(dirname "$APP_DIR")")/work"
else
  TASK_WORK="$APP_DIR/work"
fi
ENV_DIR="$TASK_WORK/audio-review-venv"
export AUDIO_REVIEW_DATA_DIR="$TASK_WORK/audio-review-runtime"
PYTHON_BIN=""
for candidate in "${AUDIO_REVIEW_PYTHON:-}" "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3"; do
  if [[ -n "$candidate" && -x "$candidate" ]] && "$candidate" -c 'import sys; assert sys.version_info >= (3, 12)' 2>/dev/null; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  print "需要 Python 3.12 或更新版本。请设置 AUDIO_REVIEW_PYTHON 后再启动。"
  exit 1
fi
if "$PYTHON_BIN" - <<'PY'
from urllib.request import build_opener, ProxyHandler
import json,sys
try:
 r=build_opener(ProxyHandler({})).open('http://127.0.0.1:8765/api/health',timeout=2)
 data=json.loads(r.read())
 sys.exit(0 if data.get('model') == 'DNSMOS P.835 · regular' else 1)
except Exception: sys.exit(1)
PY
then
  print "声检已经在运行。请打开 http://127.0.0.1:8765"
  exit 0
fi
if [[ ! -x "$ENV_DIR/bin/python" ]]; then
  mkdir -p "$TASK_WORK"
  "$PYTHON_BIN" -m venv "$ENV_DIR"
fi
if ! "$ENV_DIR/bin/python" -c 'import fastapi, uvicorn, multipart, numpy, soundfile, onnxruntime, imageio_ffmpeg' 2>/dev/null; then
  print "首次启动，正在安装本地运行组件……"
  "$ENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.lock.txt"
fi
cd "$APP_DIR"
"$ENV_DIR/bin/python" app.py --install-model
print "请打开 http://127.0.0.1:8765。关闭此窗口或按 Ctrl+C 可停止服务。"
exec "$ENV_DIR/bin/python" app.py
