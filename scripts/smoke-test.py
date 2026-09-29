"""Check container execution with synthetic tones, not prediction accuracy."""
from __future__ import annotations

import io
import json
import math
import os
import struct
import time
from urllib.request import Request, ProxyHandler, build_opener
import uuid
import wave

BASE = os.environ.get('SMOKE_BASE_URL', 'http://127.0.0.1:8001').rstrip('/')
client = build_opener(ProxyHandler({}))


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(BASE + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'} if data else {})
    with client.open(request, timeout=15) as response:
        return json.load(response)


def main():
    for attempt in range(30):
        try:
            health = api('/api/health')
            if health.get('ready'):
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError('Container health check timed out')
    assert health['evaluator_version'] == '2.0' and health['workers'] == 2

    audio = io.BytesIO()
    with wave.open(audio, 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        output.writeframes(b''.join(struct.pack('<h', round(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                                  for i in range(12 * 16000)))
    boundary = uuid.uuid4().hex
    payload = bytearray()
    for filename in ('synthetic-a.wav', 'synthetic-copy.wav'):
        payload.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'.encode())
        payload.extend(audio.getvalue())
        payload.extend(b'\r\n')
    payload.extend(f'--{boundary}--\r\n'.encode())
    request = Request(BASE + '/api/upload', data=payload,
                      headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    with client.open(request, timeout=15) as response:
        ids = [item['id'] for item in json.load(response)['items']]
    assert len(api('/api/run', {'ids': ids})['submitted']) == 2
    for attempt in range(120):
        rows = [api('/api/reviews/' + identifier) for identifier in ids]
        assert not any(row['status'] == 'error' for row in rows), rows
        if all(row['status'] == 'done' for row in rows):
            break
        time.sleep(.5)
    else:
        raise RuntimeError('Audio jobs timed out')
    assert all(row['result']['quality']['scope'] == 'fast' and
               row['result']['quality']['window_count'] == 2 and
               1 <= row['result']['quality']['overall'] <= 5 for row in rows)
    assert rows[0]['result']['quality'] == rows[1]['result']['quality']
    assert rows[1]['result']['processing']['cache_hit']
    saved = api('/api/reviews/' + ids[0] + '/human', {'ratings': {'clarity': 4}}, 'PUT')
    assert saved['review']['ratings']['clarity'] == 4
    repeated = api('/api/run', {'ids': ids})
    assert repeated['reused'] == ids
    assert api('/api/reviews/' + ids[0])['review']['ratings']['clarity'] == 4
    assert not api('/api/reviews/' + ids[1])['review']
    print('PASS: health, FFmpeg decode, ONNX scoring, duplicate reuse, independent human review')
    print('Synthetic tones check execution only; this is not a prediction-accuracy benchmark.')


if __name__ == '__main__':
    main()
