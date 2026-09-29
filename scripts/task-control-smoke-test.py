"""Verify real task pause/resume only in an explicitly disposable API instance.

Run ``exercise``, restart the disposable container, then run ``verify-restart``
with the same TASK_CONTROL_REPORT_DIR. This script does not restart containers,
delete records, or change any existing records in the test instance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError
import uuid


spec = importlib.util.spec_from_file_location(
    'parallel_fixture_helpers', Path(__file__).with_name('parallel-smoke-test.py'))
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
api, upload, fixture = helpers.api, helpers.upload, helpers.fixture
WINDOW_SECONDS = 144160 / 16000
LONG_SECONDS = int(os.environ.get('TASK_CONTROL_SECONDS', '90'))
if not 60 <= LONG_SECONDS <= 600:
    raise RuntimeError('TASK_CONTROL_SECONDS must be between 60 and 600 for observable model progress.')
FULL_WINDOWS = math.floor(LONG_SECONDS - WINDOW_SECONDS) + 2


def counters(row):
    return {key: row.get('task', {}).get(key) for key in
            ('session_id', 'processed_windows', 'scored_windows', 'total_windows', 'resume_count')}


class Verification:
    def __init__(self, folder):
        self.folder = folder
        self.path = folder / 'task-control-summary.json'
        self.report = {'passed': False, 'checks': [], 'snapshots': [],
                       'note': 'Synthetic tones verify task control and continuation, not perceptual score accuracy.'}
        self.started = time.monotonic()
        self.last_external = 0.0

    def save(self):
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n')

    def check(self, condition, label, evidence=None):
        assert condition, (label, evidence)
        self.report['checks'].append({'label': label, 'evidence': evidence})
        self.save()
        print('PASS: ' + label, flush=True)

    def snapshot(self, ids):
        document = api('/api/reviews?compact=true')
        rows = {row['id']: row for row in document['items']}
        self.external()
        chosen = {identifier: rows[identifier] for identifier in ids}
        observation = {'queue': document['queue'], 'items': {
            identifier: {key: row.get(key) for key in ('status', 'progress', 'stage', 'task')}
            for identifier, row in chosen.items()}}
        if not self.report['snapshots'] or self.report['snapshots'][-1]['observation'] != observation:
            self.report['snapshots'].append({'seconds': round(time.monotonic() - self.started, 3),
                                             'observation': observation})
            self.save()
        assert all(row['status'] != 'error' for row in chosen.values()), chosen
        return chosen, document['queue']

    def wait(self, ids, predicate, label, seconds=180):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rows, queue = self.snapshot(ids)
            if predicate(rows, queue):
                return rows, queue
            time.sleep(.04)
        raise AssertionError('Timed out: ' + label)

    def external(self):
        url = os.environ.get('TASK_CONTROL_EXISTING_SERVICE_URL')
        if url and time.monotonic() - self.last_external >= 2:
            with helpers.CLIENT.open(url, timeout=10) as response:
                assert response.status == 200, response.status
            self.report.setdefault('existing_service_checks', []).append(
                {'seconds': round(time.monotonic() - self.started, 3), 'status': 200})
            self.last_external = time.monotonic()

    def rejected(self, path, body, expected):
        try:
            api(path, body)
        except HTTPError as error:
            assert error.code == expected, (path, error.code, error.read().decode())
        else:
            raise AssertionError('Request unexpectedly succeeded: ' + path)

    def frozen(self, ids, seconds=1):
        rows, _ = self.wait(ids, lambda rows, queue: all(row['status'] == 'paused' for row in rows.values()),
                            'All requested files reach a pause safe point', seconds=30)
        before = {identifier: (row['status'], row['progress'], counters(row))
                  for identifier, row in rows.items()}
        assert all(row['status'] == 'paused' for row in rows.values()), rows
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            current, _ = self.snapshot(ids)
            assert before == {identifier: (row['status'], row['progress'], counters(row))
                              for identifier, row in current.items()}, (before, current)
            time.sleep(.08)
        return rows

    def unchanged(self, baseline):
        current = {row['id']: api('/api/reviews/' + row['id']) for row in baseline}
        self.check(all(current[row['id']] == row for row in baseline),
                   'Every pre-existing record, result and human review is unchanged',
                   {'records': len(baseline)})


def ready():
    end = time.monotonic() + 60
    while time.monotonic() < end:
        try:
            health = api('/api/health')
            if health.get('ready'):
                assert health.get('task_pause') is True, health
                assert health['workers'] in (1, 2) and health['model_threads'] == 1, health
                return health
        except OSError:
            pass
        time.sleep(.25)
    raise AssertionError('Disposable task-control instance did not become ready')


def result(row):
    assert row['status'] == 'done', row
    quality = row['result']['quality']
    assert quality['scope'] == 'full' and quality['window_count'] == FULL_WINDOWS, quality
    assert quality['skipped_low_energy_windows'] == 0, quality
    assert quality['model_sha256'] == helpers.MODEL_SHA256, quality
    assert 1 <= quality['overall'] <= 5, quality


def exercise(test, health):
    listing = api('/api/reviews?compact=true')
    assert listing['queue']['active_jobs'] == listing['queue']['queued_jobs'] == 0, listing['queue']
    baseline = [api('/api/reviews/' + row['id']) for row in listing['items']]
    test.report['health'] = health
    test.report['baseline_ids'] = [row['id'] for row in baseline]
    for path in ('/api/tasks/pause', '/api/tasks/resume'):
        test.rejected(path, {'ids': []}, 422)
        test.rejected(path, {'ids': ['missing'] * 201}, 422)
    test.check(True, 'Pause and resume reject empty and over-200 ID batches')

    with tempfile.TemporaryDirectory(prefix='audio-task-control-fixtures-') as temporary:
        folder = Path(temporary)
        a, b, queued, probe = [folder / name for name in ('long-a.wav', 'long-b.wav', 'queued.wav', 'probe.wav')]
        for path, seconds, frequency in ((a, LONG_SECONDS, 440), (b, LONG_SECONDS, 790),
                                         (queued, 12, 1130), (probe, 12, 1370)):
            fixture(path, seconds, frequency)
        imported = upload([a, b, queued, probe, a])
    a, b, queued, probe, alias = [row['id'] for row in imported]
    test.check(imported[0]['sha256'] == imported[4]['sha256']
               and len({row['sha256'] for row in imported[:4]}) == 4,
               'Fixtures include distinct jobs and two separate identical-content records')
    api('/api/reviews/' + a + '/human', {'ratings': {'clarity': 4}, 'reviewer': 'pause original'}, 'PUT')
    api('/api/reviews/' + alias + '/human', {'ratings': {'clarity': 2}, 'reviewer': 'pause duplicate'}, 'PUT')
    blockers = [a, b][:health['workers']]
    submitted = api('/api/run', {'ids': blockers + [queued], 'scope': 'full', 'force': True})
    test.check(submitted['submitted'] == blockers + [queued], 'Long jobs fill the configured slots and a third waits')
    rows, queue = test.wait(blockers + [queued], lambda rows, queue:
                           all(rows[identifier]['status'] == 'processing'
                               and rows[identifier]['task']['processed_windows'] >= 5 for identifier in blockers)
                           and rows[queued]['status'] == 'queued', 'Model windows plus a queued file')
    test.rejected('/api/tasks/pause', {'ids': [queued, 'missing-' + uuid.uuid4().hex]}, 404)
    test.check(api('/api/reviews/' + queued)['status'] == 'queued',
               'An unknown ID rejects the complete pause batch before changing a valid queued file')

    paused = api('/api/tasks/pause', {'ids': [queued, queued]})
    test.check(paused['paused'] == [queued] and not paused['skipped'],
               'Queued pause deduplicates IDs and takes effect immediately', paused)
    repeat = api('/api/tasks/pause', {'ids': [queued]})
    test.check(not repeat['paused'] and repeat['skipped'] == [queued], 'Repeated pause is idempotent', repeat)
    preserved = api('/api/reviews/' + queued)
    test.rejected('/api/tasks/resume', {'ids': [queued, 'missing-' + uuid.uuid4().hex]}, 404)
    test.check(api('/api/reviews/' + queued) == preserved, 'Unknown resume ID leaves the whole valid batch unchanged')
    normal = api('/api/run', {'ids': [queued], 'scope': 'fast', 'force': True})
    test.check(not normal['submitted'] and not normal['reused']
               and api('/api/reviews/' + queued)['status'] == 'paused',
               'The ordinary run API does not override an explicitly paused file')
    resumed = api('/api/tasks/resume', {'ids': [queued]})
    test.check(resumed['resumed'] == [queued] and not resumed['skipped'], 'A queued file can be resumed', resumed)
    test.wait(blockers + [queued], lambda rows, queue: rows[queued]['status'] == 'queued', 'Resumed file waits for a slot')
    paused = api('/api/tasks/pause', {'ids': blockers + [queued]})
    test.check(set(paused['paused']) == set(blockers + [queued]), 'A batch pauses running and queued files together', paused)
    rows, queue = test.wait(blockers + [queued], lambda rows, queue:
                           all(row['status'] == 'paused' for row in rows.values())
                           and queue['active_jobs'] == 0, 'Paused workers release their execution slots')
    checkpoint = {identifier: dict(rows[identifier]['task']) for identifier in blockers}
    test.frozen(blockers + [queued])
    test.check(queue['paused_files'] >= len(blockers) + 1 and queue['pausing_jobs'] == 0,
               'Paused progress and window counters stay frozen; no worker remains occupied', queue)

    api('/api/run', {'ids': [probe], 'scope': 'fast', 'force': True})
    test.wait([probe] + blockers, lambda rows, queue:
              rows[probe]['status'] == 'done' and all(rows[identifier]['status'] == 'paused' for identifier in blockers),
              'A new short job completes while long jobs stay paused')
    test.check(True, 'A new real model job uses the slot released by a paused job')
    api('/api/tasks/resume', {'ids': [a]})
    # Joining the original content job must not replace the original checkpoint.
    api('/api/run', {'ids': [alias], 'scope': 'full', 'force': True})
    rows, _ = test.wait([a, alias], lambda rows, queue:
                       all(row['status'] == 'processing' for row in rows.values())
                       and rows[a]['task']['processed_windows'] > checkpoint[a]['processed_windows'],
                       'A resumed original continues with a duplicate consumer')
    test.check(rows[a]['task']['session_id'] == checkpoint[a]['session_id']
               and rows[a]['task']['total_windows'] == checkpoint[a]['total_windows'],
               'Resume retains the session and advances beyond its completed windows', rows[a]['task'])
    api('/api/tasks/pause', {'ids': [alias]})
    frozen_alias = test.frozen([alias], .5)[alias]
    completed, _ = test.wait([a, alias], lambda rows, queue:
                            rows[a]['status'] == 'done' and rows[alias]['status'] == 'paused',
                            'An unpaused duplicate completes independently')
    test.check(completed[alias]['progress'] == frozen_alias['progress']
               and counters(completed[alias]) == counters(frozen_alias),
               'Pausing one identical-content record never pauses the other or changes frozen progress')
    final_a = api('/api/reviews/' + a)
    result(final_a)
    test.check(not final_a['result']['processing']['cache_hit']
               and final_a['result']['processing']['resume_count'] >= 1,
               'Resumed original completes its real model job from its checkpoint')
    resume_targets = [alias, queued] + ([b] if b in blockers else [])
    resumed = api('/api/tasks/resume', {'ids': resume_targets})
    test.check(set(resumed['resumed']) == set(resume_targets), 'Batch resume restarts paused work and reuses a completed identical result', resumed)
    if b in blockers:
        continued, _ = test.wait([b], lambda rows, queue: rows[b]['status'] == 'processing'
                                  and rows[b]['task']['processed_windows'] >= checkpoint[b]['processed_windows'],
                                  'The second worker reloads its checkpoint')
        test.check(continued[b]['task']['session_id'] == checkpoint[b]['session_id'],
                   'The second worker resumes its own checkpoint session')
    test.wait(resume_targets, lambda rows, queue: all(row['status'] == 'done' for row in rows.values()),
              'Resumed batch finishes')
    final_alias = api('/api/reviews/' + alias)
    test.check(final_alias['result']['processing']['cache_hit'] is True
               and final_alias['result']['quality'] == final_a['result']['quality'],
               'A paused duplicate resumes from the completed identical result')
    test.check(api('/api/reviews/' + a)['review']['ratings']['clarity'] == 4
               and final_alias['review']['ratings']['clarity'] == 2,
               'Both records retain their independent human ratings')
    if b in blockers:
        final_b = api('/api/reviews/' + b)
        result(final_b)
        test.check(final_b['result']['processing']['resume_count'] >= 1,
                   'The second worker completes from its restored checkpoint')
    for path, key in (('/api/tasks/pause', 'paused'), ('/api/tasks/resume', 'resumed')):
        response = api(path, {'ids': [a, alias]})
        test.check(not response[key] and response['skipped'] == [a, alias],
                   'Completed records are skipped by ' + key, response)
    test.unchanged(baseline)

    # Leave one actively computed session paused so the caller can restart only
    # its disposable test container and verify durable continuation afterwards.
    api('/api/run', {'ids': [b], 'scope': 'full', 'force': True})
    test.wait([b], lambda rows, queue: rows[b]['status'] == 'processing'
              and rows[b]['task']['processed_windows'] >= 5, 'Fresh restart checkpoint')
    api('/api/tasks/pause', {'ids': [b]})
    restart_row = test.frozen([b])[b]
    state = {'id': b, 'row': api('/api/reviews/' + b), 'task': restart_row['task'],
             'baseline': baseline, 'instance_health': health}
    (test.folder / 'task-control-restart-state.json').write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    test.check(True, 'A persisted paused session is ready for a disposable process/container restart', restart_row['task'])
    test.report['passed'] = True
    test.report['restart_verification_pending'] = True
    test.save()


def verify_restart(test, health):
    state = json.loads((test.folder / 'task-control-restart-state.json').read_text())
    identifier = state['id']
    previous = json.loads(test.path.read_text())
    test.report = previous
    test.report['passed'] = False
    test.report['snapshots'] = []
    row = api('/api/reviews/' + identifier)
    test.check(row['status'] == 'paused' and row['progress'] == state['row']['progress']
               and row['task'] == state['task'] and row['review'] == state['row']['review'],
               'Process restart retains paused state, progress, completed windows and human review', row['task'])
    test.frozen([identifier])
    resumed = api('/api/tasks/resume', {'ids': [identifier, identifier]})
    test.check(resumed['resumed'] == [identifier] and not resumed['skipped'],
               'Restarted session resumes once after duplicate ID removal', resumed)
    continued, _ = test.wait([identifier], lambda rows, queue: rows[identifier]['status'] == 'processing'
                              and rows[identifier]['task']['processed_windows'] >= state['task']['processed_windows'],
                              'Restarted session reloads completed model windows')
    test.check(continued[identifier]['task']['session_id'] == state['task']['session_id'],
               'Restarted evaluation retains its persisted checkpoint session')
    test.wait([identifier], lambda rows, queue: rows[identifier]['status'] == 'done', 'Restarted session completes')
    completed = api('/api/reviews/' + identifier)
    result(completed)
    test.check(completed['result']['processing']['resume_count'] >= 1
               and not completed['result']['processing']['cache_hit'],
               'The persisted session completes remaining model windows after restart', completed['result']['processing'])
    test.unchanged(state['baseline'])
    test.report['passed'] = True
    test.report['restart_verification_pending'] = False
    test.save()


def main():
    if os.environ.get('TASK_CONTROL_TEST_DISPOSABLE') != '1':
        raise RuntimeError('Set TASK_CONTROL_TEST_DISPOSABLE=1 only for a disposable test instance.')
    folder_value = os.environ.get('TASK_CONTROL_REPORT_DIR')
    if not folder_value:
        raise RuntimeError('TASK_CONTROL_REPORT_DIR is required to preserve restart verification evidence.')
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('exercise', 'verify-restart'), default='exercise')
    args = parser.parse_args()
    folder = Path(folder_value)
    folder.mkdir(parents=True, exist_ok=True)
    test = Verification(folder)
    try:
        health = ready()
        if args.phase == 'exercise':
            exercise(test, health)
        else:
            verify_restart(test, health)
        test.report.setdefault('phase_elapsed_seconds', {})[args.phase] = round(time.monotonic() - test.started, 3)
        test.save()
        print(json.dumps({'passed': test.report['passed'], 'checks': len(test.report['checks']),
                          'restart_verification_pending': test.report['restart_verification_pending']}), flush=True)
    except Exception as error:
        test.report['error'] = str(error)
        test.save()
        raise


if __name__ == '__main__':
    main()
