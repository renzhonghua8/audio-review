"""Verify mode changes and deletion using only newly created disposable records.

Existing records and prepared playback are read and compared, never modified.
The caller controls the disposable container and its resource monitor.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
import uuid


spec = importlib.util.spec_from_file_location(
    'record_action_helpers', Path(__file__).with_name('parallel-smoke-test.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
api, upload, fixture = helpers.api, helpers.upload, helpers.fixture
SECONDS = 45
WINDOW_SECONDS = 144160 / 16000
FULL_WINDOWS = math.floor(SECONDS - WINDOW_SECONDS) + 2
SAMPLE_WINDOWS = math.ceil(SECONDS / WINDOW_SECONDS)


class Verification:
    def __init__(self, folder, data):
        self.folder, self.data = folder, data
        self.path = folder / 'record-actions-summary.json'
        self.probe = helpers.PlaybackVerification(api, helpers.CLIENT, helpers.BASE, None, local_data=data)
        self.report = {'passed': False, 'checks': [], 'observations': [], 'existing_service_checks': [],
                       'note': 'Synthetic recordings verify task lifecycle and data preservation, not prediction accuracy.'}
        self.started = time.monotonic()
        self.last_external = 0.0

    def save(self):
        self.path.write_text(json.dumps(self.report, indent=2) + '\n')

    def check(self, condition, label, evidence=None):
        assert condition, (label, evidence)
        self.report['checks'].append({'label': label, 'evidence': evidence})
        self.save()
        print('PASS: ' + label, flush=True)

    def external(self):
        url = os.environ.get('RECORD_ACTIONS_EXISTING_SERVICE_URL')
        if url and time.monotonic() - self.last_external >= 1:
            with helpers.CLIENT.open(url, timeout=10) as response:
                assert response.status == 200, response.status
            self.report['existing_service_checks'].append({'time': time.time(), 'status': 200})
            self.last_external = time.monotonic()

    def wait(self, identifiers, predicate, label, seconds=120):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            document = api('/api/reviews?compact=true')
            chosen = {row['id']: row for row in document['items'] if row['id'] in identifiers}
            assert not any(row['status'] == 'error' for row in chosen.values()), chosen
            self.external()
            if predicate(chosen, document['queue']):
                self.report['observations'].append({'label': label,
                    'seconds': round(time.monotonic() - self.started, 3),
                    'queue': document['queue'], 'rows': chosen})
                self.save()
                return chosen, document['queue']
            time.sleep(.04)
        raise AssertionError('Timed out: ' + label)

    def rejected(self, path, body=None, method=None, expected=404):
        try:
            api(path, body, method)
        except HTTPError as error:
            assert error.code == expected, (path, error.code)
        else:
            raise AssertionError('Request unexpectedly succeeded: ' + path)

    def source_path(self, identifier):
        with sqlite3.connect(f'file:{self.data / "reviews.sqlite3"}?mode=ro', uri=True) as database:
            name = database.execute('SELECT stored_name FROM reviews WHERE id=?', (identifier,)).fetchone()[0]
        path = self.data / 'uploads' / name
        assert path.is_file() and not path.is_symlink(), str(path)
        return path

    def wait_removed(self, paths):
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if not any(path.exists() for path in paths):
                return
            self.external()
            time.sleep(.1)
        raise AssertionError('Deleted files remain after their workers and references have exited: '
                             + ', '.join(str(path) for path in paths if path.exists()))

    def playback_ready(self, identifier):
        api('/api/playback/' + identifier + '/prepare', method='POST')
        until = time.monotonic() + 30
        while time.monotonic() < until:
            status = api('/api/playback/' + identifier + '/status')
            assert status['state'] != 'error', status
            if status['state'] == 'ready':
                return status
            self.external()
            time.sleep(.1)
        raise AssertionError('Compatible playback was not prepared')


def exercise(test):
    until = time.monotonic() + 60
    while time.monotonic() < until:
        try:
            health = api('/api/health')
            if health.get('ready'):
                break
        except OSError:
            pass
        time.sleep(.25)
    else:
        raise AssertionError('Disposable instance did not become ready')
    assert health['workers'] == 2 and health['model_threads'] == 1, health
    listing = api('/api/reviews?compact=true')
    assert listing['queue']['active_jobs'] == listing['queue']['queued_jobs'] == 0, listing['queue']
    baseline = [api('/api/reviews/' + row['id']) for row in listing['items']]
    caches = {}
    for row in baseline:
        status = api('/api/playback/' + row['id'] + '/status')
        if status['state'] == 'ready':
            caches[status['cache_key']] = test.probe.file_identity(status['cache_key'])
    test.report['baseline_ids'] = [row['id'] for row in baseline]
    test.report['baseline_cache_keys'] = list(caches)
    test.report['health'] = health
    for body in ({'ids': []}, {'ids': ['missing'] * 201}):
        test.rejected('/api/reviews/delete', body, expected=422)
    test.check(True, 'Deletion rejects empty and oversized batches')

    with tempfile.TemporaryDirectory(prefix='record-action-fixtures-') as temporary:
        folder = Path(temporary)
        shared, other, switching, uploaded = [folder / name for name in
                                            ('shared.wav', 'other.wav', 'switching.wav', 'uploaded.wav')]
        for path, seconds, frequency in ((shared, SECONDS, 1753), (other, SECONDS, 2089),
                                        (switching, SECONDS, 2297), (uploaded, 12, 2473)):
            fixture(path, seconds, frequency)
        imported = upload([shared, shared, other, switching, uploaded])
    owner, alias, other, switching, uploaded = [row['id'] for row in imported]
    source_paths = {row['id']: test.source_path(row['id']) for row in imported}
    test.check(imported[0]['sha256'] == imported[1]['sha256']
               and len({row['sha256'] for row in imported}) == 4,
               'New fixtures include a source-owning record and an independently rated duplicate')
    api('/api/reviews/' + owner + '/human', {'ratings': {'clarity': 4}, 'reviewer': 'deleted source owner'}, 'PUT')
    api('/api/reviews/' + alias + '/human', {'ratings': {'clarity': 2}, 'reviewer': 'surviving duplicate'}, 'PUT')
    api('/api/reviews/' + switching + '/human', {'ratings': {'clarity': 3}, 'reviewer': 'mode change'}, 'PUT')
    untouched = api('/api/reviews/' + uploaded)
    missing = 'missing-' + uuid.uuid4().hex
    test.rejected('/api/reviews/delete', {'ids': [uploaded, missing]})
    test.check(api('/api/reviews/' + uploaded) == untouched and source_paths[uploaded].is_file(),
               'One unknown ID rejects the whole deletion batch before any valid record or source changes')
    test.rejected('/api/reviews/' + missing, method='DELETE')

    api('/api/run', {'ids': [switching], 'scope': 'full', 'force': True})
    test.wait([switching], lambda rows, queue: rows[switching]['status'] == 'processing'
              and rows[switching]['task']['scored_windows'] >= 6, 'Old full-detail mode computes real windows')
    api('/api/tasks/pause', {'ids': [switching]})
    test.wait([switching], lambda rows, queue: rows[switching]['status'] == 'paused'
              and queue['active_jobs'] == 0, 'The old mode reaches a completed pause safe point')
    previous = api('/api/reviews/' + switching)
    test.rejected('/api/tasks/resume', {'ids': [switching], 'scope': 'unknown'}, expected=422)
    test.check(api('/api/reviews/' + switching) == previous,
               'An invalid replacement mode leaves the entire paused checkpoint unchanged')
    resumed = api('/api/tasks/resume', {'ids': [switching], 'scope': 'sample'})
    test.check(resumed['resumed'] == resumed['scope_changed'] == [switching],
               'Resume explicitly acknowledges the newly selected evaluation mode', resumed)
    fresh = api('/api/reviews/' + switching)
    new_task = fresh['task']
    test.check(fresh['scope'] == 'sample' and new_task.get('session_id')
               and new_task['session_id'] != previous['task']['session_id'],
               'Replacement evaluation has its own new session instead of restoring the old checkpoint',
               {'old_task': previous['task'], 'new_task': new_task})
    test.wait([switching], lambda rows, queue: rows[switching]['status'] == 'done', 'The new sample mode finishes')
    changed = api('/api/reviews/' + switching)
    quality, processing = changed['result']['quality'], changed['result']['processing']
    test.check(changed['scope'] == quality['scope'] == 'sample'
               and quality['window_count'] == SAMPLE_WINDOWS
               and processing['resume_count'] == 0 and not processing['cache_hit']
               and changed['review'] == previous['review']
               and test.probe.download('/api/audio/' + switching)['sha256'] == changed['sha256'],
               'Changing mode starts a fresh model session with no old windows, retaining the original and human score',
               {'old_task': previous['task'], 'new_task': new_task, 'quality_windows': quality['window_count'],
                'processing': processing})

    playback = test.playback_ready(owner)
    key = playback['cache_key']
    shared_identity = test.probe.file_identity(key)
    shared_headers = test.probe.headers(playback['stream_url'])
    api('/api/run', {'ids': [owner, alias, other], 'scope': 'full', 'force': True})
    test.wait([owner, alias, other], lambda rows, queue:
              all(row['status'] == 'processing' and row['task']['scored_windows'] >= 6 for row in rows.values())
              and queue['active_jobs'] == 2, 'Two real jobs run while duplicate records share one source job')
    removed = api('/api/reviews/' + owner, method='DELETE')
    test.check(removed['deleted'] == [owner] and imported[0]['sha256'] in removed['retained_digests']
               and key not in removed['cache_keys_removed'],
               'Deleting the active source owner retains the duplicate consumer and shared playback', removed)
    test.rejected('/api/reviews/' + owner)
    test.rejected('/api/audio/' + owner)
    surviving = api('/api/reviews/' + alias)
    alias_playback = api('/api/playback/' + alias + '/status')
    test.check(surviving['status'] in {'processing', 'done'}
               and surviving['review']['ratings']['clarity'] == 2
               and alias_playback['state'] == 'ready'
               and test.probe.same_file(test.probe.file_identity(key), shared_identity)
               and test.probe.headers(alias_playback['stream_url']) == shared_headers,
               'The surviving duplicate still scores and serves the identical saved MP3 after its source owner is deleted')
    batch = api('/api/reviews/delete', {'ids': [other, uploaded, other]})
    test.check(batch['deleted'] == [other, uploaded],
               'Batch deletion deduplicates IDs and handles a running job together with an uploaded file', batch)
    for identifier in (other, uploaded):
        test.rejected('/api/reviews/' + identifier)
        test.rejected('/api/audio/' + identifier)
    test.wait([alias], lambda rows, queue: rows[alias]['status'] == 'done' and queue['active_jobs'] == 0,
              'The surviving shared job completes and cancelled workers release their slots')
    completed = api('/api/reviews/' + alias)
    test.check(completed['result']['quality']['scope'] == 'full'
               and completed['result']['quality']['window_count'] == FULL_WINDOWS
               and not completed['result']['processing']['cache_hit']
               and completed['review']['ratings']['clarity'] == 2
               and test.probe.download('/api/audio/' + alias)['sha256'] == completed['sha256'],
               'Deletion of the original consumer leaves the duplicate full model result, source bytes and independent rating valid')
    test.wait_removed([source_paths[owner], source_paths[other], source_paths[uploaded]])
    test.check(True, 'Deleted source files are cleaned after all source jobs release them')
    final = api('/api/reviews/delete', {'ids': [alias, switching]})
    test.check(final['deleted'] == [alias, switching] and key in final['cache_keys_removed'],
               'Deleting the final digest reference releases its saved compatible-file cache', final)
    test.wait_removed([source_paths[alias], source_paths[switching], test.data / 'playback' / (key + '.mp3')])
    after = [api('/api/reviews/' + row['id']) for row in baseline]
    test.check(after == baseline and {row['id'] for row in api('/api/reviews?compact=true')['items']}
               == {row['id'] for row in baseline},
               'All pre-existing records, paused checkpoints, automatic results and human ratings remain exactly unchanged',
               {'records': len(baseline)})
    test.check(all(test.probe.same_file(test.probe.file_identity(cache_key), identity)
                   for cache_key, identity in caches.items()),
               'Every pre-existing prepared recording retains its saved file identity and bytes',
               {'prepared_recordings': len(caches)})
    test.external()
    assert api('/api/health')['ready']
    test.report['passed'] = True
    test.report['elapsed_seconds'] = round(time.monotonic() - test.started, 3)
    test.save()


def main():
    if os.environ.get('RECORD_ACTIONS_TEST_DISPOSABLE') != '1':
        raise RuntimeError('Set RECORD_ACTIONS_TEST_DISPOSABLE=1 only for a disposable instance.')
    if urlsplit(helpers.BASE).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('The destructive synthetic-fixture test is restricted to a local disposable instance.')
    folder = Path(os.environ['RECORD_ACTIONS_REPORT_DIR'])
    folder.mkdir(parents=True, exist_ok=True)
    test = Verification(folder, Path(os.environ.get('AUDIO_REVIEW_DATA_DIR', '/data')))
    try:
        exercise(test)
        print(json.dumps({'passed': test.report['passed'], 'checks': len(test.report['checks']),
                          'elapsed_seconds': test.report['elapsed_seconds']}), flush=True)
    except Exception as error:
        test.report['error'] = str(error)
        test.save()
        raise


if __name__ == '__main__':
    main()
