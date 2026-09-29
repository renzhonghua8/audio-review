"""Prepare and verify a real interrupted upgrade in a disposable container.

Run the existing smoke test first, then ``--phase prepare``. Replace only the
owned container with the host deployment script and run ``--phase verify``.
The shared /data state crosses that replacement; existing reviews are read only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time


spec = importlib.util.spec_from_file_location(
    'interrupt_upgrade_fixture_helpers', Path(__file__).with_name('parallel-smoke-test.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
api = helpers.api
STATE_PATH = Path('/data/ci-interrupt-state.json')
REPORT_PATH = Path('/data/ci-interrupt-summary.json')
LONG_SECONDS = 180
MIN_WINDOWS = 3


class Verification:
    def __init__(self, phase):
        self.phase = phase
        self.started = time.monotonic()
        self.last_external = 0.0
        self.report = {'passed': False, 'upgrade_verification_pending': True,
                       'checks': [], 'snapshots': [], 'existing_service_checks': [],
                       'note': 'Synthetic tones verify interruption and data preservation, not prediction accuracy.'}
        if phase == 'verify':
            self.report = json.loads(REPORT_PATH.read_text())
            self.report['passed'] = False

    def save(self):
        REPORT_PATH.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')

    def check(self, condition, label, evidence=None):
        assert condition, (label, evidence)
        self.report['checks'].append({'phase': self.phase, 'label': label, 'evidence': evidence})
        self.save()
        print('PASS: ' + label, flush=True)

    def external(self):
        url = os.environ.get('INTERRUPT_UPGRADE_EXISTING_SERVICE_URL')
        if url and time.monotonic() - self.last_external >= .5:
            with helpers.CLIENT.open(url, timeout=5) as response:
                assert response.status == 200, response.status
            self.report['existing_service_checks'].append(
                {'phase': self.phase, 'seconds': round(time.monotonic() - self.started, 3), 'status': 200})
            self.last_external = time.monotonic()

    def ready(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self.external()
            try:
                health = api('/api/health')
                if health.get('ready'):
                    self.check(health['workers'] == 2 and health['model_threads'] == 1
                               and health['evaluator_version'] == '2.0'
                               and health['model_sha256'] == helpers.MODEL_SHA256,
                               'The real local model is ready with two workers and one model thread', health)
                    self.report.setdefault('health', {})[self.phase] = health
                    return health
            except OSError:
                pass
            time.sleep(.25)
        raise AssertionError('Disposable interrupted-upgrade instance did not become ready')

    def original(self, row):
        digest, size = hashlib.sha256(), 0
        with helpers.CLIENT.open(helpers.BASE + '/api/audio/' + row['id'], timeout=15) as response:
            assert response.status == 200, response.status
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        self.external()
        self.check(digest.hexdigest() == row['sha256'] and size == row['size'],
                   'The original audio bytes match the recorded SHA-256 and size',
                   {'id': row['id'], 'sha256': digest.hexdigest(), 'bytes': size})

    def baseline(self, rows):
        for previous in rows:
            current = api('/api/reviews/' + previous['id'])
            self.check(current == previous,
                       'A pre-existing completed record, result and human review are exactly unchanged',
                       {'id': previous['id'], 'sha256': previous['sha256'],
                        'has_human_review': bool(previous['review'])})
            self.original(current)

    def wait_processing(self, identifier):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            document = api('/api/reviews?compact=true')
            row = next(row for row in document['items'] if row['id'] == identifier)
            observation = {'status': row['status'], 'progress': row['progress'],
                           'stage': row['stage'], 'task': row['task'], 'queue': document['queue']}
            if not self.report['snapshots'] or self.report['snapshots'][-1]['observation'] != observation:
                self.report['snapshots'].append(
                    {'seconds': round(time.monotonic() - self.started, 3), 'observation': observation})
                self.save()
            self.external()
            assert row['status'] not in {'done', 'error', 'paused', 'pausing'}, row
            task = row['task']
            if (row['status'] == 'processing' and task.get('processed_windows', 0) >= MIN_WINDOWS
                    and task['processed_windows'] < task['total_windows']):
                self.check(document['queue']['active_jobs'] == 1 and task.get('force') is True
                           and task['scored_windows'] >= MIN_WINDOWS and task['total_windows'] > 100,
                           'A forced full-scope real model job is still processing after several windows',
                           observation)
                return api('/api/reviews/' + identifier)
            time.sleep(.08)
        raise AssertionError('The long real model job did not show processing window progress')


def prepare(test):
    test.ready()
    listing = api('/api/reviews?compact=true')
    test.check(listing['queue']['active_jobs'] == listing['queue']['queued_jobs'] == 0
               and listing['queue']['pausing_jobs'] == 0,
               'The initial smoke-test instance has no active or queued tasks', listing['queue'])
    baseline = [api('/api/reviews/' + row['id']) for row in listing['items']]
    test.check(len(baseline) >= 2 and all(row['status'] == 'done' and row['result'] for row in baseline)
               and any(row['review'].get('ratings', {}).get('clarity') == 4 for row in baseline),
               'Completed model results and an independent human review form the read-only baseline',
               {'ids': [row['id'] for row in baseline]})
    for row in baseline:
        test.original(row)

    with tempfile.TemporaryDirectory(prefix='audio-interrupt-upgrade-fixture-') as temporary:
        path = Path(temporary) / 'interrupt-upgrade-long.wav'
        helpers.fixture(path, LONG_SECONDS, 1237)
        with path.open('rb') as source:
            original_sha = hashlib.file_digest(source, 'sha256').hexdigest()
        imported = helpers.upload([path])[0]
    identifier = imported['id']
    test.check(imported['sha256'] == original_sha
               and original_sha not in {row['sha256'] for row in baseline},
               'A distinct 180-second original is uploaded without reusing baseline content',
               {'id': identifier, 'sha256': original_sha, 'seconds': LONG_SECONDS})
    saved = api('/api/reviews/' + identifier + '/human',
                {'ratings': {'clarity': 3}, 'reviewer': 'interrupt upgrade CI'}, 'PUT')
    submitted = api('/api/run', {'ids': [identifier], 'scope': 'full', 'force': True})
    test.check(submitted['submitted'] == [identifier] and not submitted['reused'],
               'The long full-scope job is submitted with forced real evaluation', submitted)
    processing = test.wait_processing(identifier)
    test.check(processing['status'] == 'processing' and processing['review'] == saved['review'],
               'The target remains active with its independent human score of three',
               {'id': identifier, 'task': processing['task'], 'review': saved['review']})
    state = {'schema_version': 1, 'baseline': baseline,
             'target': {'id': identifier, 'sha256': original_sha,
                        'review': saved['review'], 'processing': processing}}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    test.report['baseline_ids'] = [row['id'] for row in baseline]
    test.report['target_id'] = identifier
    test.report['prepare_passed'] = True


def verify(test):
    state = json.loads(STATE_PATH.read_text())
    assert state['schema_version'] == 1, state['schema_version']
    test.ready()
    target = state['target']
    row = api('/api/reviews/' + target['id'])
    test.check(row['status'] == 'uploaded' and row['progress'] == 0 and row['task'] == {}
               and row['result'] is None and row['error'] is None,
               'The interrupted processing task returns to uploaded and is ready for a new evaluation',
               {'id': row['id'], 'status': row['status'], 'progress': row['progress'], 'task': row['task']})
    test.check(row['sha256'] == target['sha256'] and row['review'] == target['review']
               and row['review']['ratings']['clarity'] == 3,
               'The interrupted original identity and its human review survive replacement exactly',
               {'id': row['id'], 'sha256': row['sha256'], 'review': row['review']})
    test.original(row)
    test.baseline(state['baseline'])
    listing = api('/api/reviews?compact=true')
    test.check({item['id'] for item in listing['items']}
               == {item['id'] for item in state['baseline']} | {target['id']},
               'Replacement preserves every record without adding or deleting rows',
               {'records': len(listing['items'])})
    test.check(listing['queue']['active_jobs'] == listing['queue']['queued_jobs'] == 0
               and listing['queue']['pausing_jobs'] == 0,
               'The upgraded API is healthy and leaves no interrupted job occupying a worker', listing['queue'])
    test.external()
    test.report['target_status_after_upgrade'] = row['status']
    test.report['upgrade_verification_pending'] = False
    test.report['passed'] = True


def main():
    if os.environ.get('INTERRUPT_UPGRADE_TEST_DISPOSABLE') != '1':
        raise RuntimeError('Set INTERRUPT_UPGRADE_TEST_DISPOSABLE=1 only for the disposable upgrade test instance.')
    if helpers.BASE != 'http://127.0.0.1:8001':
        raise RuntimeError('Run inside the disposable container against http://127.0.0.1:8001.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('prepare', 'verify'), required=True)
    args = parser.parse_args()
    test = Verification(args.phase)
    try:
        if args.phase == 'prepare':
            prepare(test)
        else:
            verify(test)
        test.report.setdefault('phase_elapsed_seconds', {})[args.phase] = round(time.monotonic() - test.started, 3)
        test.save()
        print(json.dumps({'phase': args.phase, 'passed': test.report['passed'],
                          'prepare_passed': test.report['prepare_passed'],
                          'upgrade_verification_pending': test.report['upgrade_verification_pending']}), flush=True)
    except Exception as error:
        test.report['error'] = str(error)
        test.save()
        raise


if __name__ == '__main__':
    main()
