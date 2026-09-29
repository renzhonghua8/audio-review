"""Monitor a disposable Linux container's cgroup, then verify its fixed limits."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import subprocess
import time


def inspect(container):
    return json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]


def number(path):
    try:
        value = path.read_text().strip()
        return int(value) if value != 'max' else None
    except (OSError, ValueError):
        return None


def pairs(path):
    try:
        return {key: int(value) for key, value in (line.split() for line in path.read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def monitor(container, output):
    output.mkdir(parents=True, exist_ok=True)
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    info = inspect(container)
    pid = info['State']['Pid']
    assert pid > 0, info['State']
    entries = [line.split(':', 2) for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()]
    unified = next((relative for _, controllers, relative in entries if not controllers), None)
    if unified is not None:
        version = 2
        memory = Path('/sys/fs/cgroup') / unified.lstrip('/')
        pids = memory
    else:
        version = 1
        memory = Path('/sys/fs/cgroup/memory') / next(
            relative for _, controllers, relative in entries if 'memory' in controllers.split(',')).lstrip('/')
        pids = Path('/sys/fs/cgroup/pids') / next(
            relative for _, controllers, relative in entries if 'pids' in controllers.split(',')).lstrip('/')
    assert memory.is_dir(), str(memory)
    metadata = {'cgroup_version': version, 'initial_pid': pid, 'image_id': info['Image'],
                'host_config': {key: info['HostConfig'].get(key)
                                for key in ('Memory', 'MemorySwap', 'NanoCpus', 'CpuShares', 'PidsLimit')}}
    def kernel_limits():
        return {'memory_max_bytes': number(memory / 'memory.max') if version == 2 else None,
                'swap_max_bytes': number(memory / 'memory.swap.max') if version == 2 else None}

    metadata['kernel_limits_initial'] = kernel_limits()
    maxima = {key: 0 for key in ('current_bytes', 'kernel_peak_bytes', 'anon_bytes', 'file_bytes',
                                  'pids_current', 'oom', 'oom_kill', 'swap_bytes')}
    count = 0

    def read_sample():
        stats = pairs(memory / 'memory.stat')
        events = pairs(memory / ('memory.events' if version == 2 else 'memory.oom_control'))
        current = number(memory / ('memory.current' if version == 2 else 'memory.usage_in_bytes'))
        combined = number(memory / 'memory.memsw.usage_in_bytes') if version == 1 else None
        return {'time': time.time(), **kernel_limits(), 'current_bytes': current,
                'kernel_peak_bytes': number(memory / ('memory.peak' if version == 2 else 'memory.max_usage_in_bytes')),
                'anon_bytes': stats.get('anon' if version == 2 else 'total_rss'),
                'file_bytes': stats.get('file' if version == 2 else 'total_cache'),
                'pids_current': number(pids / 'pids.current'), 'oom': events.get('oom'),
                'oom_kill': events.get('oom_kill'),
                'swap_bytes': number(memory / 'memory.swap.current') if version == 2 else
                    max(0, combined - current) if combined is not None and current is not None else None}

    with (output / 'resource-samples.jsonl').open('w') as samples:
        while running and not (output / 'stop-monitor').exists():
            row = read_sample()
            for key in maxima:
                if row[key] is not None:
                    maxima[key] = max(maxima[key], row[key])
            samples.write(json.dumps(row) + '\n')
            samples.flush()
            count += 1
            time.sleep(.2)
        # Capture counters after the stop signal as well, including any final OOM event.
        row = read_sample()
        for key in maxima:
            if row[key] is not None:
                maxima[key] = max(maxima[key], row[key])
        samples.write(json.dumps(row) + '\n')
        count += 1
    info = inspect(container)
    state = info['State']
    final_limits = kernel_limits()
    configured_max = final_limits['memory_max_bytes']
    summary = {**metadata, 'sample_count': count, 'maxima': maxima,
               'kernel_limits_final': final_limits,
               'peak_over_limit_bytes': max(0, maxima['kernel_peak_bytes'] - configured_max)
                   if configured_max is not None else None,
               'final_state': {key: state.get(key) for key in ('Running', 'OOMKilled', 'Status', 'ExitCode')},
               'health': state.get('Health', {}).get('Status')}
    (output / 'resource-summary.json').write_text(json.dumps(summary, indent=2) + '\n')


def verify(output):
    summary = json.loads((output / 'resource-summary.json').read_text())
    limits = summary['host_config']
    expected = 768 * 1024 * 1024
    assert limits['Memory'] == limits['MemorySwap'] == expected, limits
    assert limits['NanoCpus'] == 500000000 and limits['CpuShares'] == 128 and limits['PidsLimit'] == 256, limits
    assert summary['cgroup_version'] == 2, summary
    expected_kernel_limits = {'memory_max_bytes': expected, 'swap_max_bytes': 0}
    for name in ('kernel_limits_initial', 'kernel_limits_final'):
        assert name in summary, 'Missing kernel-limit measurements; rerun the monitor rather than infer legacy values.'
        assert summary[name] == expected_kernel_limits, summary[name]
    assert summary['sample_count'] > 10, summary
    assert summary['final_state']['Running'] and not summary['final_state']['OOMKilled'], summary
    maxima = {key: 0 for key in summary['maxima']}
    sample_count, over_limit_samples = 0, 0
    with (output / 'resource-samples.jsonl').open() as samples:
        for line in samples:
            row = json.loads(line)
            assert {key: row.get(key) for key in expected_kernel_limits} == expected_kernel_limits, row
            # Reject missing or invalid statistics instead of turning missing counters into zero.
            assert all(type(row.get(key)) is int and row[key] >= 0 for key in maxima), row
            for key in maxima:
                maxima[key] = max(maxima[key], row[key])
            sample_count += 1
            over_limit_samples += row['current_bytes'] > expected
    assert sample_count == summary['sample_count'] and maxima == summary['maxima'], summary
    assert maxima['oom'] == maxima['oom_kill'] == 0, summary
    assert maxima['swap_bytes'] == 0, summary
    assert 0 < maxima['current_bytes'] <= maxima['kernel_peak_bytes'], summary
    assert maxima['pids_current'] <= 256, summary
    # memory.max may be exceeded temporarily. Linux specifies no universal byte tolerance:
    # https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files
    # Verify the kernel's controls and execution outcomes; report the observed overage.
    peak_over_limit = max(0, maxima['kernel_peak_bytes'] - expected)
    assert summary.get('peak_over_limit_bytes') == peak_over_limit, summary
    print(json.dumps(summary, indent=2))
    print(f'Observed peak over configured memory.max: {peak_over_limit} bytes; '
          f'current-usage samples above that limit: {over_limit_samples}.')
    print('PASS: Docker and kernel memory limit are 768 MiB, kernel swap limit is zero, '
          'CPU quota is 0.5; no OOM or swap; container remained running.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('monitor', 'verify'))
    parser.add_argument('output', type=Path)
    parser.add_argument('--container')
    args = parser.parse_args()
    if args.mode == 'monitor':
        if not args.container:
            parser.error('--container is required for monitor')
        monitor(args.container, args.output)
    else:
        verify(args.output)
