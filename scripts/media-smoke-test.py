"""Exercise real format recognition, original scoring, and compatible MP3 playback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import uuid

BASE = os.environ.get('SMOKE_BASE_URL', 'http://127.0.0.1:8001').rstrip('/')
CLIENT = build_opener(ProxyHandler({}))


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(BASE + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'} if data else {})
    with CLIENT.open(request, timeout=30) as response:
        return json.load(response)


def upload(path, name=None, expected=200):
    boundary = uuid.uuid4().hex
    payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
               f'filename="{name or path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    payload += path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    request = Request(BASE + '/api/upload', data=payload,
                      headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    try:
        with CLIENT.open(request, timeout=30) as response:
            status, result = response.status, json.load(response)
    except HTTPError as error:
        status, result = error.code, json.load(error)
    assert status == expected, (name or path.name, status, result)
    return result['items'][0] if status == 200 else result


def wait(path, terminal, timeout=90):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        status = api(path)
        phase = status.get('state', status.get('status'))
        assert phase != 'error', status
        if phase == terminal:
            return status
        time.sleep(.2)
    raise RuntimeError('Timed out: ' + path)


def main():
    assert os.environ.get('FORMAT_TEST_DISPOSABLE') == '1', 'Use only an explicitly disposable test instance.'
    for _ in range(40):
        try:
            if api('/api/health')['ready']:
                break
        except OSError:
            pass
        time.sleep(.5)
    else:
        raise RuntimeError('Test service is not ready')
    ffmpeg = os.environ.get('IMAGEIO_FFMPEG_EXE') or shutil.which('ffmpeg')
    if not ffmpeg:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoders = subprocess.run([ffmpeg, '-hide_banner', '-encoders'], capture_output=True,
                              text=True, check=True, timeout=15).stdout
    specs = [
        ('wav', 'pcm_s16le'), ('mp3', 'libmp3lame'), ('m4a', 'aac'), ('aac', 'aac'),
        ('flac', 'flac'), ('ogg', 'libvorbis'), ('opus', 'libopus'), ('aiff', 'pcm_s16be'),
        ('caf', 'pcm_s16le'), ('wma', 'wmav2'), ('wv', 'wavpack'), ('tta', 'tta'),
        ('au', 'pcm_s16be'), ('ac3', 'ac3'), ('webm', 'libopus'), ('mka', 'flac'),
        ('mov', 'pcm_s16le'), ('3gp', 'aac'),
    ]
    checked = []
    with tempfile.TemporaryDirectory(prefix='audio-formats-') as folder:
        root = Path(folder)
        fixtures = []
        for index, (extension, codec) in enumerate(specs):
            path = root / f'tone-{index}.{extension}'
            rate = 48000 if codec == 'libopus' else 44100
            codec_args = ['-c:a', codec]
            if codec == 'libvorbis' and 'libvorbis' not in encoders:
                codec_args = ['-c:a', 'vorbis', '-strict', '-2', '-ac', '2']
            command = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                       '-f', 'lavfi', '-i', f'sine=frequency={300 + index * 17}:duration=3',
                       '-threads', '1', *codec_args, '-ar', str(rate), str(path)]
            subprocess.run(command, check=True, timeout=30)
            fixtures.append((path, path.name))
        alac = root / 'lossless-alac.m4a'
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'sine=frequency=701:duration=3',
                        '-threads', '1', '-c:a', 'alac', str(alac)], check=True, timeout=30)
        fixtures.append((alac, alac.name))
        video = root / 'video-with-audio.mp4'
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'color=black:size=32x32:rate=1:duration=3',
                        '-f', 'lavfi', '-i', 'sine=frequency=750:duration=3',
                        '-threads', '1', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(video)],
                       check=True, timeout=30)
        fixtures.append((video, video.name))
        amr = root / 'synthetic-parameters.amr'
        # 150 valid AMR-NB mode-0 frames. Codec parameters are zero; no real voice is recorded.
        amr.write_bytes(b'#!AMR\n' + (b'\x04' + bytes(12)) * 150)
        fixtures.append((amr, amr.name))
        fixtures.extend([(fixtures[0][0], 'recording'), (fixtures[0][0], 'recording.unusual'),
                         (fixtures[0][0], 'RECORDING.WAV')])
        rows = [(upload(path, name), hashlib.sha256(path.read_bytes()).hexdigest()) for path, name in fixtures]
        ids = [row['id'] for row, _ in rows]
        rated = api('/api/reviews/' + ids[0] + '/human', {'ratings': {'clarity': 4}, 'notes': 'synthetic-format-check'}, 'PUT')
        api('/api/run', {'ids': ids, 'scope': 'fast'})
        for row, digest in rows:
            result = wait('/api/reviews/' + row['id'], 'done', timeout=150)
            assert result['result']['metrics']['duration'] > 2.5, result
            assert result['sha256'] == digest
            original_result = result['result']
            original_review = result['review']
            api('/api/playback/' + row['id'] + '/prepare', method='POST')
            ready = wait('/api/playback/' + row['id'] + '/status', 'ready')
            assert ready['format'] == 'mp3' and ready['progress'] == 100
            request = Request(BASE + ready['stream_url'], headers={'Range':'bytes=0-63'})
            with CLIENT.open(request, timeout=30) as response:
                prefix = response.read()
                assert response.status == 206 and len(prefix) == 64
                assert response.headers.get('Content-Type') == 'audio/mpeg'
                assert response.headers.get('Content-Range', '').startswith('bytes 0-63/')
            with CLIENT.open(BASE + ready['stream_url'], timeout=30) as response:
                compatible = root / 'compatible.mp3'
                compatible.write_bytes(response.read())
            subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                            '-i', str(compatible), '-f', 'null', '-'], check=True, timeout=30)
            with CLIENT.open(BASE + '/api/audio/' + row['id'], timeout=30) as response:
                assert hashlib.sha256(response.read()).hexdigest() == digest
            after = api('/api/reviews/' + row['id'])
            assert after['result'] == original_result and after['review'] == original_review
            checked.append(row['filename'])
        assert api('/api/reviews/' + ids[0])['review'] == rated['review']
        invalid = root / 'broken.wav'
        invalid.write_text('This is not an audio recording.')
        before = len(api('/api/reviews')['items'])
        upload(invalid, expected=400)
        manifest = root / 'playlist.mp3'
        manifest.write_text("ffconcat version 1.0\nfile '/etc/passwd'\n")
        upload(manifest, expected=400)
        silent_video = root / 'no-audio.mp4'
        subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'color=black:size=32x32:rate=1:duration=1',
                        '-threads', '1', '-c:v', 'libx264', str(silent_video)], check=True, timeout=30)
        upload(silent_video, expected=400)
        assert len(api('/api/reviews')['items']) == before
    report = {'passed':True, 'recordings':len(checked), 'formats':checked,
              'checks':['content detection', 'original model scoring', 'compatible MP3 decode',
                        'Range/206 and audio/mpeg', 'original bytes and ratings preserved',
                        'no extension / unusual extension / upper case', 'invalid media and playlists rejected']}
    report_dir = os.environ.get('PARALLEL_REPORT_DIR')
    if report_dir:
        (Path(report_dir) / 'media-format-summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
