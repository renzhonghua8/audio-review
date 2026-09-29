"""Exercise two real model jobs in a disposable instance, never production data."""
from __future__ import annotations

import array
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import uuid
import wave


BASE = os.environ.get('SMOKE_BASE_URL', 'http://127.0.0.1:8001').rstrip('/')
CLIENT = build_opener(ProxyHandler({}))
MODEL_SHA256 = '269fbebdb513aa23cddfbb593542ecc540284a91849ac50516870e1ac78f6edd'


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(BASE + path, data=data, method=method,
                      headers={'Content-Type': 'application/json'} if data else {})
    with CLIENT.open(request, timeout=30) as response:
        return json.load(response)


def fixture(path, seconds, frequency):
    """Write bounded one-second blocks; distinct tones cannot share the SHA cache."""
    rate = 48000
    block = array.array('h')
    for index in range(rate):
        block.extend((round(3500 * math.sin(2 * math.pi * frequency * index / rate)),
                      round(2600 * math.sin(2 * math.pi * (frequency + 170) * index / rate))))
    if os.sys.byteorder != 'little':
        block.byteswap()
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, rate, 0, 'NONE', 'not compressed'))
        for _ in range(seconds):
            output.writeframesraw(block.tobytes())


def upload(paths):
    """Stream multipart file data from the host, without a large in-memory body."""
    boundary = uuid.uuid4().hex
    heads = [(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
              f'filename="{path.name}"\r\nContent-Type: audio/wav\r\n\r\n').encode()
             for path in paths]
    tail = f'--{boundary}--\r\n'.encode()
    size = sum(len(head) + path.stat().st_size + 2 for head, path in zip(heads, paths)) + len(tail)
    parsed = urlsplit(BASE)
    connection_class = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=180)
    try:
        connection.putrequest('POST', parsed.path.rstrip('/') + '/api/upload')
        connection.putheader('Content-Type', f'multipart/form-data; boundary={boundary}')
        connection.putheader('Content-Length', size)
        connection.endheaders()
        for head, path in zip(heads, paths):
            connection.send(head)
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
            connection.send(b'\r\n')
        connection.send(tail)
        response = connection.getresponse()
        document = json.loads(response.read())
        assert response.status == 200, (response.status, document)
        assert len(document['items']) == len(paths), document
        return document['items']
    finally:
        connection.close()


def check_result(row, seconds, windows):
    assert row['status'] == 'done', row
    result = row['result']
    metrics, quality = result['metrics'], result['quality']
    assert result['evaluator_version'] == '2.0', result
    assert metrics['sample_rate'] == 48000 and metrics['channels'] == 2, metrics
    assert abs(metrics['duration'] - seconds) < .01, metrics
    assert quality['scope'] == 'fast' and quality['status'] == 'scored', quality
    assert quality['window_count'] == windows and quality['skipped_low_energy_windows'] == 0, quality
    assert quality['model_sha256'] == MODEL_SHA256, quality
    assert 1 <= quality['overall'] <= 5 and not result['processing']['cache_hit'], result


def main():
    if os.environ.get('PARALLEL_TEST_DISPOSABLE') != '1':
        raise RuntimeError('Set PARALLEL_TEST_DISPOSABLE=1 only for an empty disposable test instance.')
    for attempt in range(120):
        try:
            health = api('/api/health')
            if health.get('ready'):
                break
        except OSError:
            pass
        time.sleep(.5)
    else:
        raise RuntimeError('Test instance did not become ready')
    assert health['workers'] == 2 and health['model_threads'] == 1, health
    assert not api('/api/reviews?compact=true')['items'], 'Use an empty disposable instance.'

    report_dir = os.environ.get('PARALLEL_REPORT_DIR')
    report_path = Path(report_dir) / 'parallel-api-summary.json' if report_dir else None
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'passed': False, 'health': health, 'rounds': [], 'existing_service_checks': [],
              'note': 'Synthetic tones validate execution and resource use, not prediction accuracy or all-input safety.'}

    def save_report():
        if report_path:
            report_path.write_text(json.dumps(report, indent=2) + '\n')

    existing_url = os.environ.get('PARALLEL_EXISTING_SERVICE_URL')
    def check_existing():
        if existing_url:
            with CLIENT.open(existing_url, timeout=10) as response:
                assert response.status == 200, response.status
                report['existing_service_checks'].append({'time': time.time(), 'status': response.status})

    save_report()
    check_existing()
    try:
        with tempfile.TemporaryDirectory(prefix='audio-review-parallel-fixtures-') as temporary:
            folder = Path(temporary)
            paths = [folder / name for name in ('long-a.wav', 'long-b.wav', 'short-c.wav')]
            for path, seconds, frequency in zip(paths, (600, 600, 30), (440, 790, 1130)):
                fixture(path, seconds, frequency)
            rows = upload(paths)
            ids = [row['id'] for row in rows]
            assert len({row['sha256'] for row in rows}) == 3, rows
            # SHA validation is streaming too; no fixture is read wholly into memory.
            for path, row in zip(paths, rows):
                with path.open('rb') as source:
                    assert hashlib.file_digest(source, 'sha256').hexdigest() == row['sha256']
            api('/api/reviews/' + ids[0] + '/human',
                {'ratings': {'clarity': 4}, 'reviewer': 'isolated concurrency test'}, 'PUT')

            for round_number, targets in enumerate((ids, ids[:2]), 1):
                started = time.monotonic()
                submitted = api('/api/run', {'ids': targets, 'scope': 'fast', 'force': True})
                assert submitted['submitted'] == targets and not submitted['reused'], submitted
                observation = {'round': round_number, 'force': True, 'trace': [],
                               'two_processing_observed': False, 'third_queued_observed': False,
                               'both_scoring_observed': False, 'progress_values': {identifier: [] for identifier in ids[:2]}}
                report['rounds'].append(observation)
                last_snapshot, last_existing = None, 0.0
                deadline = started + 600
                while time.monotonic() < deadline:
                    document = api('/api/reviews?compact=true')
                    current = {row['id']: row for row in document['items']}
                    relevant = [current[identifier] for identifier in targets]
                    assert not any(row['status'] == 'error' for row in relevant), relevant
                    pair = [current[identifier] for identifier in ids[:2]]
                    # List rows and queue counters are read separately; ignore a transition
                    # snapshot instead of treating a completion race as a scheduler failure.
                    pair_active = (all(row['status'] == 'processing' for row in pair)
                                   and document['queue']['active_jobs'] == 2)
                    if pair_active:
                        observation['two_processing_observed'] = True
                        if (round_number == 1 and current[ids[2]]['status'] == 'queued'
                                and document['queue']['queued_jobs'] == 1):
                            observation['third_queued_observed'] = True
                        if all(row['progress'] >= 35 for row in pair):
                            observation['both_scoring_observed'] = True
                    for row in pair:
                        if row['status'] == 'processing':
                            values = observation['progress_values'][row['id']]
                            if row['progress'] not in values:
                                values.append(row['progress'])
                    snapshot = {'queue': document['queue'],
                                'items': [{'id': row['id'], 'status': row['status'], 'progress': row['progress'],
                                           'stage': row['stage']} for row in relevant]}
                    if snapshot != last_snapshot:
                        observation['trace'].append({'elapsed_seconds': round(time.monotonic() - started, 3), **snapshot})
                        last_snapshot = snapshot
                        save_report()
                    if time.monotonic() - last_existing >= 5:
                        check_existing()
                        last_existing = time.monotonic()
                    if all(row['status'] == 'done' for row in relevant):
                        break
                    time.sleep(.25)
                else:
                    raise RuntimeError('Parallel model jobs did not finish within 600 seconds')
                assert observation['two_processing_observed'], observation
                assert observation['both_scoring_observed'], observation
                assert all(len(values) >= 3 and max(values) >= 35
                           for values in observation['progress_values'].values()), observation
                if round_number == 1:
                    assert observation['third_queued_observed'], observation
                completed = [api('/api/reviews/' + identifier) for identifier in targets]
                for index, row in enumerate(completed):
                    check_result(row, 600 if index < 2 else 30, 67 if index < 2 else 4)
                assert completed[0]['review']['ratings']['clarity'] == 4, completed[0]['review']
                assert not completed[1]['review'], completed[1]['review']
                observation['elapsed_seconds'] = round(time.monotonic() - started, 3)
                observation['results'] = [{'id': row['id'], 'overall': row['result']['quality']['overall'],
                                           'window_count': row['result']['quality']['window_count'],
                                           'processing': row['result']['processing']} for row in completed]
                assert api('/api/health')['ready']
                check_existing()
                save_report()
                print(json.dumps({key: value for key, value in observation.items() if key != 'trace'}), flush=True)
            report['passed'] = True
            save_report()
            print('PASS: two distinct real model jobs overlap and progress; a third waits; forced reruns preserve independent human review.')
            print(report['note'])
    except Exception as error:
        report['error'] = str(error)
        save_report()
        raise


if __name__ == '__main__':
    main()
