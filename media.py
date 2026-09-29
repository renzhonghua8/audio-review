"""Recognize local media by decoded content, independent of its filename."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
import subprocess
import threading

import imageio_ffmpeg

# These inputs describe other files, scripts, live sources, or devices rather
# than one uploaded recording. Allow the installed build's other demuxers.
EXTERNAL_INPUTS = {
    'concat', 'hls', 'applehttp', 'dash', 'sdp', 'rtsp', 'sap', 'imf', 'dvdvideo',
    'image2', 'image2pipe', 'sbg', 'vapoursynth', 'avisynth', 'avfoundation',
    'lavfi', 'libcdio', 'alsa', 'oss', 'dshow', 'decklink', 'fbdev', 'kmsgrab',
    'gdigrab', 'x11grab', 'video4linux2', 'v4l2', 'openal', 'jack', 'pulse',
}
probe_lock = threading.Lock()


@lru_cache(maxsize=1)
def local_input_options() -> tuple[str, ...]:
    result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-demuxers'],
                            capture_output=True, text=True, timeout=15)
    formats = set()
    for line in result.stdout.splitlines():
        match = re.match(r'^\s*D[.\w]*\s+([A-Za-z0-9_,]+)\s', line)
        if match:
            formats.update(match.group(1).split(','))
    formats.difference_update(EXTERNAL_INPUTS)
    if result.returncode or not {'wav', 'mp3', 'mov'} <= formats:
        raise RuntimeError('无法读取本地媒体解码能力，请检查 FFmpeg 安装。')
    return ('-protocol_whitelist', 'file,pipe', '-format_whitelist', ','.join(sorted(formats)))


def validate_audio(source: Path) -> None:
    """Decode a bounded opening sample with the same decoder used by scoring."""
    with probe_lock:
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
                   '-threads', '1', '-filter_threads', '1', *local_input_options(),
                   '-i', str(source), '-map', '0:a:0', '-vn', '-t', '0.25',
                   '-ac', '1', '-ar', '8000', '-threads', '1', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('音轨识别超时，请确认文件有效后重试。') from error
        if result.returncode or not result.stdout:
            raise RuntimeError('无法读取有效音轨。请确认文件未损坏、未加密，且不是播放列表或无声轨视频。')
