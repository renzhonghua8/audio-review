"""Bounded, evaluation-priority MP3 preparation for browser playback."""
from __future__ import annotations

from collections import deque
from email.utils import formatdate
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable
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
                 filename: str | None = None) -> Response:
    """Serve original or cached media with explicit bounded byte ranges.

    Open once before computing headers, so cache eviction cannot replace or remove
    the file between determining its size and sending the requested bytes.
    """
    try:
        stream = path.open('rb')
    except (FileNotFoundError, IsADirectoryError):
        raise HTTPException(404, '本地音频文件不存在。') from None
    try:
        stat = os.fstat(stream.fileno())
        size = stat.st_size
        etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
        modified = formatdate(stat.st_mtime, usegmt=True)
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(size),
                   'ETag': etag, 'Last-Modified': modified, 'Content-Disposition': 'inline'}
        if filename:
            headers['Content-Disposition'] = f"inline; filename*=UTF-8''{quote(filename)}"
        media_type = media_type or mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
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
    def __init__(self, folder: Path, source_root: Path, evaluation_busy: Callable[[], bool]):
        self.folder = folder.resolve()
        self.source_root = source_root.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.evaluation_busy = evaluation_busy
        self._condition = threading.Condition(threading.RLock())
        self._queue = deque()
        self._entries = {}
        self._closed = False
        self._process = None
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

    def _ready(self, digest: str) -> Path | None:
        path = self._path(digest)
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
            return path
        return None

    def status(self, digest: str) -> dict:
        with self._condition:
            path = self._ready(digest)
            if path:
                os.utime(path, None)
                return {'state': 'ready', 'progress': 100, 'message': '回听文件已准备，可播放。',
                        'format': 'mp3', 'prepared_seconds': None}
            entry = self._entries.get(digest)
            if entry:
                if entry['state'] == 'ready':
                    return {'state': 'idle', 'progress': None, 'message': '回听缓存已释放，可重新准备。',
                            'format': 'mp3', 'prepared_seconds': 0}
                return {key: entry[key] for key in ('state', 'progress', 'message', 'prepared_seconds')} | {'format': 'mp3'}
            return {'state': 'idle', 'progress': None, 'message': '回听文件尚未准备。',
                    'format': 'mp3', 'prepared_seconds': 0}

    def prepare(self, source: Path, digest: str, duration: float | None = None) -> dict:
        path = source.resolve()
        if path.parent != self.source_root or source.is_symlink() or not path.is_file():
            raise PlaybackError('原音频文件不存在或路径无效，请重新导入。')
        if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
            raise PlaybackError('回听支持 200 MB 以内的有效音频，请先拆分文件。')
        self._path(digest)
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
                                     'state': 'queued', 'progress': 0, 'prepared_seconds': 0,
                                     'message': '回听文件已排队，评测任务优先运行。'}
            self._queue.append(digest)
            self._condition.notify()
            return self.status(digest)

    def cached_path(self, digest: str) -> Path | None:
        with self._condition:
            path = self._ready(digest)
            if path:
                os.utime(path, None)
            return path

    def audio_response(self, digest: str, request: Request, *, filename: str) -> Response:
        with self._condition:
            path = self.cached_path(digest)
            if path is None:
                raise HTTPException(409, '回听文件尚未准备完成，请先准备回听。')
            # Open the file while eviction is locked. Subsequent streaming keeps
            # its own descriptor even if a later preparation evicts this cache.
            return stream_audio(path, request, media_type='audio/mpeg', filename=filename)

    def _busy(self) -> bool:
        try:
            return bool(self.evaluation_busy())
        except Exception:
            # A failed priority check must not add CPU load to evaluations.
            return True

    def _set(self, digest, **values):
        with self._condition:
            self._entries[digest].update(values)

    def _wait_idle(self, digest):
        while not self._closed and self._busy():
            self._set(digest, state='waiting', message='正在评测音频；回听准备会在评测空闲后继续。')
            with self._condition:
                self._condition.wait(timeout=0.2)
        if self._closed:
            raise PlaybackError('服务已停止，请稍后重新准备回听。')

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed:
                    return
                digest = self._queue.popleft()
            try:
                self._wait_idle(digest)
                self._convert(digest)
                self._set(digest, state='ready', progress=100, message='回听文件已准备，可播放。')
            except Exception as error:
                message = str(error) if isinstance(error, PlaybackError) else '回听文件准备失败，请稍后重试。'
                self._set(digest, state='error', progress=None, message=message)

    def _reserve_space(self):
        with self._condition:
            files = sorted((path for path in self.folder.glob(f'*.{CACHE_VERSION}.mp3')
                            if path.is_file() and not path.is_symlink()), key=lambda path: path.stat().st_mtime)
            total = sum(path.stat().st_size for path in files)
            for path in files:
                if total <= MAX_CACHE_BYTES - MAX_OUTPUT_BYTES:
                    break
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                total -= size
        if shutil.disk_usage(self.folder).free < MAX_OUTPUT_BYTES + 128 * 1024 * 1024:
            raise PlaybackError('回听缓存所在磁盘可用空间不足 640 MB，请释放空间后重试。')

    def _command(self, source, destination, progress):
        return [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-max_alloc', '67108864', '-threads', '1', '-filter_threads', '1',
                '-filter_complex_threads', '1', *local_input_options(), '-i', str(source),
                '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1',
                '-t', str(MAX_DURATION_SECONDS + 1), '-ac', '2', '-ar', '44100',
                '-threads', '1', '-c:a', 'libmp3lame', '-b:a', '256k',
                '-fs', str(MAX_OUTPUT_BYTES), '-progress', str(progress), '-nostats', str(destination)]

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    def _convert(self, digest):
        self._reserve_space()
        with self._condition:
            entry = dict(self._entries[digest])
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
                    process = subprocess.Popen(self._command(entry['source'], partial, progress),
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=diagnostic, start_new_session=True)
                    with self._condition:
                        self._process = process
                    try:
                        os.setpriority(os.PRIO_PROCESS, process.pid, 19)
                    except (AttributeError, OSError):
                        pass
                    self._set(digest, state='converting', progress=0 if entry['duration'] else None,
                              message='正在准备浏览器兼容的回听文件…')
                    last = time.monotonic()
                    paused = False
                    while True:
                        current = time.monotonic()
                        if not paused:
                            active_seconds += current - last
                        last = current
                        if self._closed:
                            raise PlaybackError('服务已停止，请稍后重新准备回听。')
                        if active_seconds > MAX_ACTIVE_SECONDS:
                            raise PlaybackError('回听准备超时，请换用较短片段后重试。')
                        if partial.exists() and partial.stat().st_size >= MAX_OUTPUT_BYTES:
                            raise PlaybackError('回听文件超过 512 MB，请先拆分原音频。')
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
                        busy = self._busy()
                        if busy and not paused:
                            # Pause immediately when evaluation work arrives. Its waiting
                            # time does not consume the conversion's execution timeout.
                            if not hasattr(signal, 'SIGSTOP'):
                                raise PlaybackError('评测任务正在运行，请在评测空闲后重试回听。')
                            try:
                                os.kill(process.pid, signal.SIGSTOP)
                            except ProcessLookupError:
                                continue
                            paused = True
                            self._set(digest, state='waiting', message='正在评测音频；回听准备会在评测空闲后继续。')
                        elif not busy and paused:
                            try:
                                os.kill(process.pid, signal.SIGCONT)
                            except ProcessLookupError:
                                continue
                            paused = False
                            self._set(digest, state='converting', message='正在准备浏览器兼容的回听文件…')
                        with self._condition:
                            self._condition.wait(timeout=0.2)
                    if process.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
                        raise PlaybackError('这个文件的音轨无法转换，请确认编码受支持且文件未损坏、未加密。')
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
