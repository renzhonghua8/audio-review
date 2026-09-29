"""Shared real-playback checks for explicitly disposable concurrency tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request


IDENTITY_FIELDS = ['inode', 'mtime_ns', 'bytes', 'sha256'] + ([] if sys.platform == 'darwin' else ['ctime_ns'])


class PlaybackVerification:
    def __init__(self, api, client, base, folder, *, local_data=None):
        self.api, self.client, self.base = api, client, base
        self.folder = Path(folder) if folder else None
        self.local_data = Path(local_data) if local_data else None
        self.report = {'passed': False, 'checks': [], 'observations': [],
                       'restart_verification_pending': False,
                       'file_identity_fields': list(IDENTITY_FIELDS)}
        if self.folder:
            self.folder.mkdir(parents=True, exist_ok=True)

    def save(self):
        if self.folder:
            (self.folder / 'busy-playback-summary.json').write_text(
                json.dumps(self.report, indent=2) + '\n')

    def check(self, condition, label, evidence=None):
        assert condition, (label, evidence)
        self.report['checks'].append({'label': label, 'evidence': evidence})
        self.save()

    def queue(self, ids):
        document = self.api('/api/reviews?compact=true')
        rows = {row['id']: row for row in document['items']}
        chosen = {identifier: rows[identifier] for identifier in ids}
        assert not any(row['status'] == 'error' for row in chosen.values()), chosen
        return chosen, document['queue']

    def headers(self, url):
        with self.client.open(Request(self.base + url, method='HEAD'), timeout=30) as response:
            assert response.status == 200
            headers = dict(response.headers)
            assert response.headers.get('Content-Type') == 'audio/mpeg', headers
            assert response.headers.get('Accept-Ranges') == 'bytes', headers
            assert 'private' in response.headers.get('Cache-Control', '')
            assert 'immutable' in response.headers.get('Cache-Control', '')
            return {'bytes': int(response.headers['Content-Length']),
                    'etag': response.headers['ETag'],
                    'last_modified': response.headers.get('Last-Modified')}

    def download(self, url, destination=None):
        """Hash and optionally save in 64 KiB blocks, including original media."""
        digest, size = hashlib.sha256(), 0
        output = Path(destination).open('wb') if destination else None
        try:
            with self.client.open(self.base + url, timeout=30) as response:
                assert response.status == 200, response.status
                expected = int(response.headers['Content-Length'])
                while chunk := response.read(64 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    if output:
                        output.write(chunk)
                assert size == expected, (size, expected)
        finally:
            if output:
                output.close()
        return {'sha256': digest.hexdigest(), 'bytes': size}

    def file_identity(self, key):
        if not self.local_data:
            return None
        assert re.fullmatch(r'[a-f0-9]{64}\.mp3-1', key), key
        path = self.local_data / 'playback' / (key + '.mp3')
        assert path.is_file() and not path.is_symlink(), str(path)
        stat = path.stat()
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        return {'inode': stat.st_ino, 'mtime_ns': stat.st_mtime_ns,
                'ctime_ns': stat.st_ctime_ns, 'bytes': stat.st_size, 'sha256': digest}

    def same_file(self, before, after):
        if before is None or after is None:
            return before == after
        # macOS adds com.apple.provenance asynchronously, including after the
        # application exits. That changes ctime while preserving the media.
        # Keep reporting ctime, but Linux CI additionally requires it unchanged.
        keys = self.report['file_identity_fields']
        return {key: before[key] for key in keys} == {key: after[key] for key in keys}

    def decode(self, path):
        if not self.local_data:
            return
        ffmpeg = os.environ.get('IMAGEIO_FFMPEG_EXE') or shutil.which('ffmpeg')
        assert ffmpeg, 'A real FFmpeg decoder is required inside the test image.'
        subprocess.run(['nice', '-n', '5', ffmpeg, '-nostdin', '-hide_banner',
                        '-loglevel', 'error', '-threads', '1', '-filter_threads', '1',
                        '-filter_complex_threads', '1', '-i', str(path),
                        '-threads', '1', '-f', 'null', '-'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       check=True, timeout=60)
        self.check(True, 'The received compatible MP3 decodes with real FFmpeg')

    def exercise(self, identifier, blockers, queued):
        try:
            return self._exercise(identifier, blockers, queued)
        except Exception as error:
            self.report['error'] = str(error)
            self.save()
            raise

    def _exercise(self, identifier, blockers, queued):
        started = time.monotonic()
        before = self.api('/api/reviews/' + identifier)
        self.check(before['status'] == 'done' and before['result']
                   and before['review']['ratings']['clarity'] == 3,
                   'The listening sample already has an automatic result and an independent human rating')
        rows, queue = self.queue(blockers + [queued])
        self.check(len(blockers) == 2 and queue['active_jobs'] == 2
                   and all(rows[value]['status'] == 'processing'
                           and rows[value]['task']['scored_windows'] >= 3 for value in blockers)
                   and rows[queued]['status'] == 'queued' and queue['queued_jobs'] >= 1,
                   'Prepare begins while two real model jobs score and a third remains queued',
                   {'queue': queue, 'jobs': {value: rows[value]['task'] for value in blockers}})
        initial = self.api('/api/playback/' + identifier + '/status')
        self.check(initial['state'] == 'idle', 'This is a fresh compatible-file cache miss', initial)
        self.api('/api/playback/' + identifier + '/prepare', method='POST')
        last = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = self.api('/api/playback/' + identifier + '/status')
            assert status['state'] not in {'error', 'waiting'}, status
            if status != last:
                self.report['observations'].append({'seconds': round(time.monotonic() - started, 3),
                                                    'status': status})
                self.save()
                last = status
            if status['state'] == 'ready':
                break
            time.sleep(.1)
        else:
            raise AssertionError('Compatible playback did not become ready in 90 seconds')
        rows, queue = self.queue(blockers + [queued])
        self.check(queue['active_jobs'] + queue['queued_jobs'] > 0
                   and any(rows[value]['status'] in {'processing', 'queued'} for value in blockers + [queued]),
                   'Playback reaches ready before the model queue becomes idle',
                   {'queue': queue, 'statuses': {value: row['status'] for value, row in rows.items()},
                    'elapsed_seconds': round(time.monotonic() - started, 3)})
        key, stream_url = status['cache_key'], status['stream_url']
        self.check(status['progress'] == 100 and status['format'] == 'mp3'
                   and key == before['sha256'] + '.mp3-1'
                   and parse_qs(urlsplit(stream_url).query).get('key') == [key],
                   'The compatible URL identifies the source and encoding version', status)
        headers = self.headers(stream_url)
        self.check(headers['bytes'] == status['bytes'] > 0,
                   'HEAD supplies a stable immutable MP3 representation', headers)
        with self.client.open(Request(self.base + stream_url, headers={'Range': 'bytes=0-63'}),
                              timeout=30) as response:
            prefix = response.read(64)
            self.check(response.status == 206 and len(prefix) == 64
                       and response.headers.get('Content-Type') == 'audio/mpeg'
                       and response.headers.get('Content-Range') == f"bytes 0-63/{headers['bytes']}"
                       and response.headers.get('ETag') == headers['etag'],
                       'Bounded byte-range GET returns the compatible MP3 with HTTP 206')
        with tempfile.TemporaryDirectory(prefix='compatible-playback-check-') as folder:
            path = Path(folder) / 'received.mp3'
            received = self.download(stream_url, path)
            self.check(received['bytes'] == headers['bytes'],
                       'Whole-file GET downloads the complete compatible recording in bounded blocks', received)
            self.decode(path)
        identity = self.file_identity(key)
        self.check(identity is None or identity['sha256'] == received['sha256'],
                   'The downloaded bytes match the persistent server MP3')
        for _ in range(3):
            repeated = self.api('/api/playback/' + identifier + '/prepare', method='POST')
            self.check(repeated['state'] == 'ready' and repeated['cache_key'] == key
                       and repeated['bytes'] == received['bytes']
                       and self.headers(repeated['stream_url']) == headers
                       and self.same_file(self.file_identity(key), identity),
                       'Repeated preparation immediately reuses the same file and validators')
        original = self.download('/api/audio/' + identifier)
        self.check(original['sha256'] == before['sha256']
                   and self.api('/api/reviews/' + identifier) == before,
                   'Listening leaves original bytes, automatic results and human scores unchanged', original)
        state = {'id': identifier, 'row': before, 'cache_key': key,
                 'headers': headers, 'received': received, 'file_identity': identity}
        if self.folder and self.local_data:
            (self.folder / 'busy-playback-restart-state.json').write_text(json.dumps(state, indent=2) + '\n')
            self.report['restart_verification_pending'] = True
        self.report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        self.report['passed'] = True
        self.save()
        return self.report

    def verify_restart(self):
        try:
            self.report = json.loads((self.folder / 'busy-playback-summary.json').read_text())
            self.report.setdefault('file_identity_fields', list(IDENTITY_FIELDS))
            state = json.loads((self.folder / 'busy-playback-restart-state.json').read_text())
            self.report['passed'] = False
            actual_identity = self.file_identity(state['cache_key'])
            self.check(self.same_file(actual_identity, state['file_identity']),
                       'A process restart retains the saved MP3 identity and bytes under the platform metadata policy',
                       {'before': state['file_identity'], 'after': actual_identity})
            status = self.api('/api/playback/' + state['id'] + '/status')
            self.check(status['state'] == 'ready' and status['cache_key'] == state['cache_key'],
                       'The first post-restart status discovers the saved compatible recording without conversion', status)
            prepared = self.api('/api/playback/' + state['id'] + '/prepare', method='POST')
            self.check(prepared['state'] == 'ready' and self.headers(prepared['stream_url']) == state['headers']
                       and self.same_file(self.file_identity(state['cache_key']), state['file_identity']),
                       'Post-restart preparation reuses the file and preserves HTTP validators')
            self.check(self.download(prepared['stream_url']) == state['received']
                       and self.download('/api/audio/' + state['id'])['sha256'] == state['row']['sha256']
                       and self.api('/api/reviews/' + state['id']) == state['row'],
                       'Restart and repeated listening preserve compatible bytes, original audio and all scores')
            self.report['restart_verification_pending'] = False
            self.report['passed'] = True
            self.save()
            return self.report
        except Exception as error:
            self.report['error'] = str(error)
            self.save()
            raise
