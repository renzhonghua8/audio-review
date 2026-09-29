"""Bounded, persistent MP3 preparation for browser playback."""
from __future__ import annotations

from collections import deque
from email.utils import formatdate
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat as file_stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

import imageio_ffmpeg
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from media import local_input_options


CACHE_VERSION = 'mp3-1'
MAX_DURATION_SECONDS = 4 * 60 * 60
MAX_SOURCE_BYTES = 200 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_QUEUED_FILES = 32
MAX_ACTIVE_SECONDS = 900
_DIGEST = re.compile(r'^[a-f0-9]{64}$')


def stream_audio(path: Path, request: Request, *, media_type: str | None = None,
                 filename: str | None = None, cache_key: str | None = None) -> Response:
    """Serve original or cached media with explicit bounded byte ranges.

    Open once before computing headers, so external removal cannot change the
    file between determining its size and sending the requested bytes.
    """
    try:
        stream = path.open('rb')
    except (FileNotFoundError, IsADirectoryError):
        raise HTTPException(404, '本地音频文件不存在。') from None
    try:
        stat = os.fstat(stream.fileno())
        size = stat.st_size
        etag = f'"{cache_key}"' if cache_key else f'"{stat.st_mtime_ns:x}-{size:x}"'
        modified = formatdate(stat.st_mtime, usegmt=True)
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(size),
                   'ETag': etag, 'Last-Modified': modified, 'Content-Disposition': 'inline'}
        if cache_key:
            headers['Cache-Control'] = 'private, max-age=31536000, immutable'
        if filename:
            headers['Content-Disposition'] = f"inline; filename*=UTF-8''{quote(filename)}"
        media_type = media_type or mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
        if request.method in {'GET', 'HEAD'}:
            validators = [item.strip().removeprefix('W/')
                          for item in request.headers.get('if-none-match', '').split(',')]
            if etag in validators or '*' in validators:
                stream.close()
                headers.pop('Content-Length')
                return Response(status_code=304, headers=headers)
        start, end = 0, size - 1
        code = 200
        # HTTP Range applies to GET; HEAD reports the full representation.
        requested = request.headers.get('range') if request.method == 'GET' else None
        if request.headers.get('if-range') not in {None, etag, modified}:
            requested = None
        if requested and requested.lower().startswith('bytes='):
            valid = re.fullmatch(r'bytes=(\d*)-(\d*)', requested.strip(), flags=re.IGNORECASE) if len(requested) < 128 else None
            if valid and size > 0:
                first, last = valid.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                    valid = start <= end and start < size
                elif last and int(last) > 0:
                    start = max(0, size - int(last))
                else:
                    valid = False
            else:
                valid = False
            if not valid:
                stream.close()
                headers.update({'Content-Range': f'bytes */{size}', 'Content-Length': '0'})
                return Response(status_code=416, headers=headers, media_type=media_type)
            code = 206
            headers.update({'Content-Range': f'bytes {start}-{end}/{size}',
                            'Content-Length': str(end - start + 1)})
        if request.method == 'HEAD':
            stream.close()
            return Response(status_code=200, headers=headers, media_type=media_type)
        stream.seek(start)

        def chunks():
            remaining = max(0, end - start + 1)
            try:
                while remaining:
                    block = stream.read(min(64 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
            finally:
                stream.close()

        return StreamingResponse(chunks(), status_code=code, media_type=media_type, headers=headers,
                                 background=BackgroundTask(stream.close))
    except Exception:
        stream.close()
        raise


class PlaybackError(RuntimeError):
    """A failure with a safe, actionable message for the browser."""


class PlaybackCache:
    def __init__(self, folder: Path, source_root: Path, evaluation_busy=None):
        self.folder = folder.resolve()
        self.source_root = source_root.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        # Keep the optional argument for older integrations. User-requested
        # playback runs independently of the scoring queue within container caps.
        configured = os.environ.get('AUDIO_REVIEW_PLAYBACK_CACHE_MB', str(MAX_CACHE_BYTES // (1024 * 1024)))
        try:
            cache_mb = int(configured)
        except ValueError:
            raise ValueError('AUDIO_REVIEW_PLAYBACK_CACHE_MB 必须是 512 到 16384 的整数。') from None
        if not 512 <= cache_mb <= 16384:
            raise ValueError('AUDIO_REVIEW_PLAYBACK_CACHE_MB 必须是 512 到 16384 的整数。')
        self.max_cache_bytes = cache_mb * 1024 * 1024
        self._condition = threading.Condition(threading.RLock())
        self._queue = deque()
        self._entries = {}
        self._closed = False
        self._process = None
        self._active_entry = None
        # Interrupted conversions are never exposed as ready media.
        for path in self.folder.glob(f'*.{CACHE_VERSION}.part.mp3'):
            path.unlink(missing_ok=True)
        for path in self.folder.glob('prepare-*'):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        self._worker = threading.Thread(target=self._work, name='audio-playback', daemon=True)
        self._worker.start()

    def _path(self, digest: str) -> Path:
        if not _DIGEST.fullmatch(digest):
            raise PlaybackError('音频标识无效，请重新导入。')
        return self.folder / f'{digest}.{CACHE_VERSION}.mp3'

    def cache_key(self, digest: str) -> str:
        self._path(digest)
        return f'{digest}.{CACHE_VERSION}'

    def _ready(self, digest: str) -> Path | None:
        path = self._path(digest)
        try:
            status = path.lstat()
        except FileNotFoundError:
            return None
        if file_stat.S_ISREG(status.st_mode) and 0 < status.st_size <= MAX_OUTPUT_BYTES:
            return path
        return None

    def status(self, digest: str) -> dict:
        with self._condition:
            common = {'format': 'mp3', 'cache_key': self.cache_key(digest), 'bytes': None}
            path = self._ready(digest)
            if path:
                return {'state': 'ready', 'progress': 100, 'message': '回听文件已准备，可播放。',
                        'prepared_seconds': None} | common | {'bytes': path.stat().st_size}
            entry = self._entries.get(digest)
            if entry:
                if entry['state'] == 'ready':
                    return {'state': 'idle', 'progress': None, 'message': '已保存的回听文件不存在，请重新准备。',
                            'prepared_seconds': 0} | common
                return {key: entry[key] for key in ('state', 'progress', 'message', 'prepared_seconds')} | common
            return {'state': 'idle', 'progress': None, 'message': '回听文件尚未准备。',
                    'prepared_seconds': 0} | common

    def prepare(self, source: Path, digest: str, duration: float | None = None) -> dict:
        path = source.resolve()
        if path.parent != self.source_root or source.is_symlink() or not path.is_file():
            raise PlaybackError('原音频文件不存在或路径无效，请重新导入。')
        if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise PlaybackError('回听支持 200 MB 以内的有效音频，请先拆分文件。')
        self._path(digest)
        if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0):
            duration = None
        if duration is not None and duration > MAX_DURATION_SECONDS:
            raise PlaybackError('回听支持 4 小时以内的音频，请先拆分文件。')
        with self._condition:
            if self._closed:
                raise PlaybackError('服务正在重启，请稍后重试。')
            entry = self._entries.get(digest)
            if self._ready(digest) or (entry and entry['state'] in {'queued', 'waiting', 'converting'}):
                return self.status(digest)
            pending = sum(item['state'] in {'queued', 'waiting', 'converting'} for item in self._entries.values())
            if pending >= MAX_QUEUED_FILES:
                raise PlaybackError('回听准备队列已满，请等当前文件完成后重试。')
            # Keep bounded recent errors so another request does not hide a failure.
            for key in list(self._entries):
                if len(self._entries) < MAX_QUEUED_FILES * 2:
                    break
                if self._entries[key]['state'] in {'ready', 'error'}:
                    self._entries.pop(key)
            self._entries[digest] = {'source': path, 'duration': duration,
                                     'cancel_event': threading.Event(),
                                     'state': 'queued', 'progress': 0, 'prepared_seconds': 0,
                                     'message': '回听文件已排队，会自动准备并保存。'}
            self._queue.append(digest)
            self._condition.notify()
            return self.status(digest)

    def cached_path(self, digest: str) -> Path | None:
        with self._condition:
            return self._ready(digest)

    def references_source(self, source: Path) -> bool:
        """Keep a deleted record's source alive for shared queued/active media."""
        path = source.resolve()
        with self._condition:
            if self._active_entry and self._active_entry[1]['source'] == path:
                return True
            return any(entry['source'] == path and entry['state'] in {'queued', 'waiting', 'converting'}
                       for entry in self._entries.values())

    def audio_response(self, digest: str, request: Request, *, filename: str) -> Response:
        with self._condition:
            path = self.cached_path(digest)
            if path is None:
                raise HTTPException(409, '回听文件尚未准备完成，请先准备回听。')
            return stream_audio(path, request, media_type='audio/mpeg', filename=filename,
                                cache_key=self.cache_key(digest))

    def _set(self, digest, **values):
        with self._condition:
            entry = self._entries.get(digest)
            if entry is None or (self._active_entry and self._active_entry[0] == digest and self._active_entry[1] is not entry):
                return
            entry.update(values)

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed:
                    return
                digest = self._queue.popleft()
                entry = self._entries.get(digest)
                if entry is None:
                    continue
                self._active_entry = (digest, entry)
            try:
                self._convert(digest)
                self._set(digest, state='ready', progress=100, message='回听文件已准备，可播放。')
            except Exception as error:
                message = str(error) if isinstance(error, PlaybackError) else '回听文件准备失败，请稍后重试。'
                self._set(digest, state='error', progress=None, message=message)
            finally:
                with self._condition:
                    self._active_entry = None

    def remove(self, digest: str):
        """Cancel one content entry and remove only its compatible media."""
        destination = self._path(digest)
        with self._condition:
            entry = self._entries.pop(digest, None)
            if entry:
                entry['cancel_event'].set()
            self._queue = deque(key for key in self._queue if key != digest)
            active = self._active_entry and self._active_entry[0] == digest
            if active:
                self._active_entry[1]['cancel_event'].set()
            process = self._process if active else None
            self._condition.notify_all()
            destination.unlink(missing_ok=True)
        if process:
            self._kill(process)
        with self._condition:
            current = self._active_entry
            if not current or current[0] != digest or current[1] is entry:
                destination.with_name(f'{digest}.{CACHE_VERSION}.part.mp3').unlink(missing_ok=True)

    def _cache_bytes(self):
        total = 0
        for path in self.folder.glob(f'*.{CACHE_VERSION}.mp3'):
            try:
                status = path.lstat()
            except FileNotFoundError:
                continue
            if file_stat.S_ISREG(status.st_mode):
                total += status.st_size
        return total

    def _capacity_error(self):
        return PlaybackError(f'已保存的回听文件已接近 {self.max_cache_bytes // (1024 * 1024)} MB 容量上限；'
                             '已有文件会保留。请增加 AUDIO_REVIEW_PLAYBACK_CACHE_MB，或手动清理不需要的回听文件后重试。')

    def _reserve_space(self, duration=None):
        # 256 kbps MP3 needs about 32000 bytes/second. Extra space covers encoder
        # headers/padding; inaccurate input metadata is also checked while writing.
        required = MAX_OUTPUT_BYTES if duration is None else min(MAX_OUTPUT_BYTES, math.ceil(duration * 32000) + 2 * 1024 * 1024)
        with self._condition:
            total = self._cache_bytes()
            if total + required > self.max_cache_bytes:
                raise self._capacity_error()
        if shutil.disk_usage(self.folder).free < required + 128 * 1024 * 1024:
            raise PlaybackError(f'回听缓存所在磁盘可用空间不足 {math.ceil(required / (1024 * 1024)) + 128} MB，'
                                '已有回听文件会保留，请释放空间后重试。')
        return min(MAX_OUTPUT_BYTES, self.max_cache_bytes - total)

    def _command(self, source, destination, progress, output_limit=MAX_OUTPUT_BYTES):
        return [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-max_alloc', '67108864', '-threads', '1', '-filter_threads', '1',
                '-filter_complex_threads', '1', *local_input_options(), '-i', str(source),
                '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
                '-t', str(MAX_DURATION_SECONDS + 1), '-ac', '2', '-ar', '44100',
                '-threads', '1', '-c:a', 'libmp3lame', '-b:a', '256k',
                '-fs', str(output_limit), '-progress', str(progress), '-nostats', str(destination)]

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def _convert(self, digest):
        with self._condition:
            original_entry = self._active_entry[1] if self._active_entry and self._active_entry[0] == digest else self._entries[digest]
            entry = dict(original_entry)
        if entry['cancel_event'].is_set():
            raise PlaybackError('该音频已删除，回听准备已取消。')
        output_limit = self._reserve_space(entry['duration'])
        destination = self._path(digest)
        partial = destination.with_name(f'{digest}.{CACHE_VERSION}.part.mp3')
        process = None
        completed_seconds = 0
        active_seconds = 0
        cursor = 0
        try:
            with tempfile.TemporaryDirectory(prefix='prepare-', dir=self.folder) as work:
                progress = Path(work) / 'progress.txt'
                errors = Path(work) / 'errors.txt'
                with errors.open('wb') as diagnostic:
                    process = subprocess.Popen(self._command(entry['source'], partial, progress, output_limit),
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=diagnostic, start_new_session=True)
                    with self._condition:
                        self._process = process
                    try:
                        # Give scoring more CPU weight without starving playback
                        # under a continuous queue. Container limits still bound both.
                        os.setpriority(os.PRIO_PROCESS, process.pid, 5)
                    except (AttributeError, OSError):
                        pass
                    self._set(digest, state='converting', progress=0 if entry['duration'] else None,
                              message='正在准备浏览器兼容的回听文件…')
                    last = time.monotonic()
                    while True:
                        current = time.monotonic()
                        active_seconds += current - last
                        last = current
                        if self._closed:
                            raise PlaybackError('服务已停止，请稍后重新准备回听。')
                        if entry['cancel_event'].is_set():
                            raise PlaybackError('该音频已删除，回听准备已取消。')
                        if active_seconds > MAX_ACTIVE_SECONDS:
                            raise PlaybackError('回听准备超时，请换用较短片段后重试。')
                        if partial.exists() and partial.stat().st_size >= MAX_OUTPUT_BYTES:
                            raise PlaybackError('回听文件超过 512 MB，请先拆分原音频。')
                        if partial.exists() and partial.stat().st_size + self._cache_bytes() > self.max_cache_bytes:
                            raise self._capacity_error()
                        if shutil.disk_usage(self.folder).free < 128 * 1024 * 1024:
                            raise PlaybackError('回听缓存所在磁盘可用空间不足 128 MB；已有文件会保留，请释放空间后重试。')
                        if errors.stat().st_size > 8 * 1024 * 1024:
                            raise PlaybackError('原音频包含过多解码错误，请确认文件完整。')
                        if progress.exists():
                            if progress.stat().st_size > 4 * 1024 * 1024:
                                raise PlaybackError('回听准备未能正常结束，请换用较短片段。')
                            with progress.open('rb') as stream:
                                stream.seek(cursor)
                                block = stream.read(65536)
                                # An incomplete line is read again next time.
                                end = block.rfind(b'\n') + 1
                                cursor += end
                            for line in block[:end].splitlines():
                                if line.startswith(b'out_time_us='):
                                    try:
                                        completed_seconds = max(completed_seconds, int(line.split(b'=', 1)[1]) / 1_000_000)
                                    except ValueError:
                                        pass
                            percent = min(99, int(completed_seconds / entry['duration'] * 100)) if entry['duration'] else None
                            self._set(digest, progress=percent, prepared_seconds=round(completed_seconds, 2))
                        if completed_seconds > MAX_DURATION_SECONDS:
                            raise PlaybackError('回听支持 4 小时以内的音频，请先拆分文件。')
                        if process.poll() is not None:
                            break
                        with self._condition:
                            self._condition.wait(timeout=0.2)
                    if process.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
                        raise PlaybackError('这个文件的音轨无法转换，请确认编码受支持且文件未损坏、未加密。')
                    size = partial.stat().st_size
                    if size >= output_limit and output_limit < MAX_OUTPUT_BYTES:
                        raise self._capacity_error()
                    if size + self._cache_bytes() > self.max_cache_bytes:
                        raise self._capacity_error()
                    with self._condition:
                        if entry['cancel_event'].is_set() or self._entries.get(digest) is not original_entry:
                            raise PlaybackError('该音频已删除，回听准备已取消。')
                        partial.replace(destination)
        finally:
            if process:
                self._kill(process)
            partial.unlink(missing_ok=True)
            with self._condition:
                self._process = None

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            process = self._process
        if process:
            self._kill(process)
        self._worker.join(timeout=5)
