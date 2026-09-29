"""Local acoustic inspection and regular DNSMOS P.835 inference."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.request import urlopen

import imageio_ffmpeg
import numpy as np
import onnxruntime as ort
import soundfile as sf

from media import local_input_options

MODEL_URL = "https://raw.githubusercontent.com/microsoft/DNS-Challenge/master/DNSMOS/DNSMOS/sig_bak_ovr.onnx"
MODEL_SHA256 = "269fbebdb513aa23cddfbb593542ecc540284a91849ac50516870e1ac78f6edd"
SAMPLE_RATE = 16000
WINDOW_SAMPLES = 144160
WINDOW_SECONDS = WINDOW_SAMPLES / SAMPLE_RATE
EVALUATOR_VERSION = '2.0'
SCOPE_LABELS = {'fast': '全量快速', 'sample': '快速抽样', 'full': '全量精细'}
CALIBRATION = np.array([
    [-0.08397278, 1.22083953, 0.00524390],
    [-0.13166888, 1.60915514, -0.39604546],
    [-0.06766283, 1.11546468, 0.04602535],
], dtype=np.float64)


class EvaluationPaused(Exception):
    """Leave an evaluation at a safe point without occupying its worker."""

    info = None
    progress = None


def check_control(checkpoint):
    if checkpoint:
        checkpoint()


def install_model(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == MODEL_SHA256:
        return destination
    bundled = Path(__file__).parent / 'models' / 'sig_bak_ovr.onnx'
    if bundled.exists():
        data = bundled.read_bytes()
    else:
        with urlopen(MODEL_URL, timeout=40) as response:
            data = response.read(2_000_000)
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise RuntimeError("音质模型校验未通过，请重新下载官方模型。")
    temporary = destination.with_suffix('.download')
    temporary.write_bytes(data)
    temporary.replace(destination)
    return destination


def decode_audio(source: Path, destination: Path, *, mono_16k: bool = False, checkpoint=None) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-y', '-threads', '1', '-filter_threads', '1', *local_input_options(), '-i', str(source), '-map', '0:a:0', '-vn', '-t', '14400']
    if mono_16k:
        command += ['-ac', '1', '-ar', str(SAMPLE_RATE)]
    command += ['-threads', '1', '-c:a', 'pcm_f32le', str(destination)]
    execute_decode(command, checkpoint)


def execute_decode(command: list[str], checkpoint=None) -> None:
    process = None
    started = time.monotonic()
    try:
        check_control(checkpoint)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        while process.poll() is None:
            check_control(checkpoint)
            if time.monotonic() - started > 300:
                raise RuntimeError("音频解码超时，请换用较短片段或 WAV 文件。")
            time.sleep(.05)
        check_control(checkpoint)
        if process.returncode != 0:
            raise RuntimeError("无法读取这个文件中的音轨，请确认它是有效且未损坏的音频。")
    finally:
        if process:
            if process.poll() is None:
                process.kill()
            process.wait()


def decode_pair(source: Path, original: Path, mono: Path, *, checkpoint=None) -> None:
    # Decode the compressed stream once, keeping the native acoustic branch.
    command = [imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
               '-y', '-threads', '1', '-filter_threads', '1', *local_input_options(), '-i', str(source), '-map', '0:a:0', '-vn', '-t', '14400',
               '-threads', '1', '-c:a', 'pcm_f32le', str(original), '-map', '0:a:0', '-vn', '-t', '14400',
               '-ac', '1', '-ar', str(SAMPLE_RATE), '-threads', '1', '-c:a', 'pcm_f32le', str(mono)]
    execute_decode(command, checkpoint)


def acoustic_analysis(path: Path, progress, *, checkpoint=None) -> dict:
    with sf.SoundFile(path) as audio:
        sr, channels, frame_count = audio.samplerate, audio.channels, len(audio)
        if not frame_count:
            raise RuntimeError("音频为空，无法评测。")
        if frame_count / sr >= 14399:
            raise RuntimeError("第一版支持 4 小时以内的音频，请先拆分文件。")
        block_size = max(1, sr // 20)
        rms_frames, peak_frames = [], []
        sum_squares, sample_count, near_full_scale, dc_sum = 0.0, 0, 0, 0.0
        max_peak = 0.0
        for index, block in enumerate(audio.blocks(blocksize=block_size * 20, dtype='float32', always_2d=True)):
            check_control(checkpoint)
            values = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0)
            squares = np.square(values, dtype=np.float64)
            absolute = np.abs(values)
            energy = float(np.sum(squares))
            count = values.size
            peak = float(np.max(absolute))
            sum_squares += energy
            sample_count += count
            dc_sum += float(np.sum(values, dtype=np.float64))
            near_full_scale += int(np.count_nonzero(absolute >= 0.995))
            max_peak = max(max_peak, peak)
            complete_frames = len(values) // block_size
            if complete_frames:
                framed_squares = squares[:complete_frames * block_size].reshape(complete_frames, -1)
                framed_absolute = absolute[:complete_frames * block_size].reshape(complete_frames, -1)
                rms_frames.extend(np.sqrt(np.mean(framed_squares, axis=1)).tolist())
                peak_frames.extend(np.max(framed_absolute, axis=1).tolist())
            tail = complete_frames * block_size
            if tail < len(values):
                rms_frames.append(float(np.sqrt(np.mean(squares[tail:]))))
                peak_frames.append(float(np.max(absolute[tail:])))
            if index % 25 == 0:
                progress(10 + int(20 * min((index * block_size * 20) / frame_count, 1)), '全量声学检测')

    duration = frame_count / sr
    frame_duration = block_size / sr
    rms = np.asarray(rms_frames)
    db = 20 * np.log10(np.maximum(rms, 1e-9))
    silent = db < -55
    active = db[~silent]
    silent_spans = []
    begin = None
    for index, is_silent in enumerate(np.append(silent, False)):
        if is_silent and begin is None:
            begin = index
        elif not is_silent and begin is not None:
            start, end = begin * frame_duration, min(index * frame_duration, duration)
            if end - start >= 3:
                silent_spans.append({'start': round(start, 2), 'end': round(end, 2)})
            begin = None
    peaks = np.asarray(peak_frames)
    bins = min(1000, len(peaks))
    bounds = np.linspace(0, len(peaks), bins + 1, dtype=int)
    envelope = [round(float(np.max(peaks[bounds[i]:bounds[i+1]])), 4) for i in range(bins)]
    average_db = 20 * math.log10(max(math.sqrt(sum_squares / max(sample_count, 1)), 1e-9))
    ratio = near_full_scale / max(sample_count, 1)
    findings = []
    if ratio > .001:
        index = int(np.argmax(peaks))
        findings.append({'kind': 'peak', 'start': round(index * frame_duration, 2),
                         'end': round(min((index + 1) * frame_duration, duration), 2),
                         'title': '发现较多接近满幅的样本',
                         'description': f'占比 {ratio * 100:.2f}%，请回听是否存在削波或失真。', 'level': '建议回听'})
    if average_db < -35 and len(active) > 0:
        findings.append({'kind': 'volume', 'start': 0, 'end': min(duration, 10),
                         'title': '整体声音电平偏低', 'description': '请确认实际播放音量及是否影响听清。', 'level': '建议回听'})
    for span in sorted(silent_spans, key=lambda x: x['end'] - x['start'], reverse=True)[:5]:
        findings.append({'kind': 'silence', **span, 'title': '连续低能量片段',
                         'description': f"持续 {span['end'] - span['start']:.1f} 秒，可能是静音或较轻的声音；不直接扣分。",
                         'level': '建议回听'})
    return {
        'duration': round(duration, 3), 'sample_rate': sr, 'channels': channels,
        'rms_dbfs': round(average_db, 2), 'peak_dbfs': round(20 * math.log10(max(max_peak, 1e-9)), 2),
        'near_full_scale_percent': round(ratio * 100, 4),
        'low_energy_percent': round(float(np.mean(silent)) * 100, 2),
        'active_level_spread_db': round(float(np.percentile(active, 90) - np.percentile(active, 10)), 2) if len(active) else None,
        'dc_offset': round(dc_sum / max(sample_count, 1), 6),
        'low_energy_spans': silent_spans, 'waveform': envelope, 'findings': findings,
    }


def score_regions(duration: float, scope: str) -> list[tuple[float, float]]:
    if scope in {'full', 'fast'} or duration <= 120:
        return [(0.0, duration)]
    second_start = min(max(60.0, duration / 2 - 30), duration - 60)
    return [(0.0, 60.0), (second_start, second_start + 60.0)]


def score_locations(regions: list[tuple[float, float]], scope: str) -> list[tuple[float, float, bool]]:
    locations = []
    for begin, end in regions:
        duration = end - begin
        if duration < WINDOW_SECONDS:
            locations.append((begin, end, True))
        elif scope == 'full':
            locations.extend((begin + offset, begin + offset + WINDOW_SECONDS, False)
                             for offset in range(int(math.floor(duration - WINDOW_SECONDS)) + 1))
            if end - locations[-1][1] > 1e-6:
                locations.append((end - WINDOW_SECONDS, end, False))
        else:
            # Evenly spaced windows cover the entire chosen region, including its tail.
            count = int(math.ceil(duration / WINDOW_SECONDS))
            locations.extend((float(start), float(start + WINDOW_SECONDS), False)
                             for start in np.linspace(begin, end - WINDOW_SECONDS, count))
    return locations


def read_checkpoint(path: Path, scope: str, identity: str) -> dict | None:
    """Reject damaged or incompatible checkpoints and safely start again."""
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    def integer(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        saved = json.loads(path.read_text())
        if (not isinstance(saved, dict) or type(saved.get('checkpoint_version')) is not int or saved.get('checkpoint_version') != 1 or
                saved.get('evaluator_version') != EVALUATOR_VERSION or saved.get('model_sha256') != MODEL_SHA256 or
                saved.get('scope') != scope or saved.get('source_identity') != identity):
            return None
        windows, next_index, skipped, total = (saved.get(key) for key in ('windows', 'next_index', 'skipped', 'total_windows'))
        if (not isinstance(windows, list) or not all(integer(value) for value in (next_index, skipped, total)) or
                next_index > total or len(windows) + skipped != next_index or
                not integer(saved.get('resume_count')) or not number(saved.get('elapsed_seconds')) or saved['elapsed_seconds'] < 0):
            return None
        metrics = saved.get('metrics')
        if metrics is None:
            return saved if next_index == skipped == total == 0 else None
        if not isinstance(metrics, dict):
            return None
        duration = metrics.get('duration')
        if not number(duration) or not 0 < duration < 14399:
            return None
        if any(not integer(metrics.get(key)) or metrics[key] <= 0 for key in ('sample_rate', 'channels')):
            return None
        scalars = ('rms_dbfs', 'peak_dbfs', 'near_full_scale_percent', 'low_energy_percent', 'dc_offset')
        if any(not number(metrics.get(key)) for key in scalars):
            return None
        if metrics.get('active_level_spread_db') is not None and not number(metrics.get('active_level_spread_db')):
            return None
        waveform, spans, findings = (metrics.get(key) for key in ('waveform', 'low_energy_spans', 'findings'))
        if (not isinstance(waveform, list) or not 1 <= len(waveform) <= 1000 or
                any(not number(value) or value < 0 for value in waveform) or
                not isinstance(spans, list) or not isinstance(findings, list)):
            return None
        for span in [*spans, *findings]:
            if (not isinstance(span, dict) or not number(span.get('start')) or not number(span.get('end')) or
                    not 0 <= span['start'] <= span['end'] <= duration + .02):
                return None
        if any(any(not isinstance(finding.get(key), str) for key in ('kind', 'title', 'description', 'level')) for finding in findings):
            return None
        expected_total = len(score_locations(score_regions(duration, scope), scope))
        # A pause immediately after acoustic analysis precedes location setup.
        if total != expected_total and not (total == next_index == skipped == 0 and not windows):
            return None
        previous_start = -1
        for window in windows:
            if (not isinstance(window, dict) or not number(window.get('start')) or not number(window.get('end')) or
                    not previous_start <= window['start'] <= window['end'] <= duration + .02 or window['start'] < 0 or
                    any(not number(window.get(key)) or not 1 <= window[key] <= 5 for key in ('overall', 'speech', 'background')) or
                    not isinstance(window.get('padded'), bool)):
                return None
            previous_start = window['start']
        return saved
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None


class Evaluator:
    def __init__(self, model_path: Path, *, threads: int = 2):
        self.model_path = model_path
        self._session = None
        self._lock = threading.Lock()
        self.threads = threads

    @property
    def ready(self):
        return self.model_path.exists()

    def session(self):
        with self._lock:
            if self._session is None:
                options = ort.SessionOptions()
                options.intra_op_num_threads = self.threads
                options.inter_op_num_threads = 1
                self._session = ort.InferenceSession(str(self.model_path), options, providers=['CPUExecutionProvider'])
            return self._session

    def evaluate(self, source: Path, folder: Path, scope: str, progress, *, checkpoint=None,
                 source_identity: str | None = None) -> dict:
        if scope not in SCOPE_LABELS:
            raise RuntimeError('请选择有效的评分模式。')
        started = time.perf_counter()
        folder.mkdir(parents=True, exist_ok=True)
        original, mono = folder / 'decoded-original.wav', folder / 'decoded-16k.wav'
        saved_path = folder / 'checkpoint.json'
        identity = source_identity or str(source.resolve())
        state = {'checkpoint_version': 1, 'evaluator_version': EVALUATOR_VERSION, 'model_sha256': MODEL_SHA256,
                 'scope': scope, 'source_identity': identity,
                 'metrics': None, 'windows': [], 'next_index': 0, 'skipped': 0, 'total_windows': 0,
                 'elapsed_seconds': 0, 'resume_count': 0}
        if saved := read_checkpoint(saved_path, scope, identity):
            state.update(saved)
            state['resume_count'] += 1
        paused = False
        try:
            check_control(checkpoint)
            progress(3, '继续读取音频' if state['resume_count'] else '读取音频', self.task_info(state))
            if state['metrics'] is None:
                decode_pair(source, original, mono, checkpoint=checkpoint)
                metrics = acoustic_analysis(original, progress, checkpoint=checkpoint)
                state['metrics'] = metrics
                original.unlink(missing_ok=True)
            else:
                # Paused jobs retain compact checkpoints, not multi-GB decoded WAVs.
                # Only the mono scoring branch needs to be read again on resume.
                decode_audio(source, mono, mono_16k=True, checkpoint=checkpoint)
                metrics = state['metrics']
            check_control(checkpoint)
            progress(32, '准备音质评分')
            if not self.ready:
                raise RuntimeError('本地 DNSMOS 音质模型未就绪，请先安装模型后重试。')
            regions = score_regions(metrics['duration'], scope)
            windows, skipped = state['windows'], state['skipped']
            locations = score_locations(regions, scope)
            state['total_windows'] = len(locations)
            progress(35 + int(60 * state['next_index'] / max(len(locations), 1)),
                     f"音质评分 {state['next_index']}/{len(locations)}", self.task_info(state))
            session = self.session()
            with sf.SoundFile(mono) as audio:
                for index in range(state['next_index'], len(locations)):
                    check_control(checkpoint)
                    start, end, padded = locations[index]
                    audio.seek(min(int(start * SAMPLE_RATE), max(0, len(audio) - 1)))
                    samples = audio.read(min(WINDOW_SAMPLES, round((end - start) * SAMPLE_RATE)), dtype='float32')
                    if not len(samples) or float(np.sqrt(np.mean(samples.astype('float64') ** 2))) < 10 ** (-55 / 20):
                        skipped += 1
                    else:
                        if len(samples) < WINDOW_SAMPLES:
                            samples = np.tile(samples, int(math.ceil(WINDOW_SAMPLES / len(samples))))[:WINDOW_SAMPLES]
                        raw = session.run(None, {'input_1': samples.reshape(1, -1).astype(np.float32)})[0][0]
                        calibrated = np.asarray([np.polyval(CALIBRATION[j], raw[j]) for j in range(3)])
                        bounded = np.clip(calibrated, 1, 5)
                        windows.append({'start': round(start, 2), 'end': round(min(end, metrics['duration']), 2),
                                        'speech': round(float(bounded[0]), 3), 'background': round(float(bounded[1]), 3),
                                        'overall': round(float(bounded[2]), 3), 'padded': padded})
                    state.update(next_index=index + 1, skipped=skipped)
                    if index % 5 == 0 or index == len(locations) - 1:
                        progress(35 + int(60 * (index + 1) / max(len(locations), 1)),
                                 f'音质评分 {index + 1}/{len(locations)}', self.task_info(state))
                    check_control(checkpoint)
            check_control(checkpoint)
            overall = float(np.mean([w['overall'] for w in windows])) if windows else None
            if windows:
                worst = sorted(windows, key=lambda item: item['overall'])
                chosen = []
                for window in worst:
                    if window['overall'] >= 2.5 or any(abs(window['start'] - w['start']) < 12 for w in chosen):
                        continue
                    chosen.append(window)
                    metrics['findings'].append({'kind': 'quality', 'start': window['start'], 'end': window['end'],
                                                'title': '音质模型预测较低的片段',
                                                'description': f"该片段整体音质 {window['overall']:.2f}/5，请结合人声、配乐回听确认。",
                                                'level': '建议回听'})
                    if len(chosen) >= 3:
                        break
            score = {
                'overall': round(overall, 3) if overall is not None else None,
                'speech': round(float(np.mean([w['speech'] for w in windows])), 3) if windows else None,
                'background': round(float(np.mean([w['background'] for w in windows])), 3) if windows else None,
                'p10': round(float(np.percentile([w['overall'] for w in windows], 10)), 3) if windows else None,
                'minimum': round(min(w['overall'] for w in windows), 3) if windows else None,
                'window_count': len(windows), 'skipped_low_energy_windows': skipped,
                'short_audio_padded': any(w['padded'] for w in windows), 'windows': windows,
                'scope': scope, 'regions': [{'start': a, 'end': b} for a, b in regions],
                'coverage_seconds': round(sum(b - a for a, b in regions), 2),
                'hop_seconds': round(1 if scope == 'full' else max(
                    ((end - begin - WINDOW_SECONDS) / (math.ceil((end - begin) / WINDOW_SECONDS) - 1)
                     for begin, end in regions if end - begin > WINDOW_SECONDS), default=WINDOW_SECONDS), 3),
                'target_hop_seconds': 1 if scope == 'full' else WINDOW_SECONDS, 'window_seconds': WINDOW_SECONDS,
                'window_placement': '1 秒滑窗' if scope == 'full' else '连续覆盖所选区间，边界适当重叠',
                'model': 'DNSMOS P.835 · regular', 'model_sha256': MODEL_SHA256,
                'calibration': '官方普通模式多项式；尚未按实际中文音频与人工评分校准',
                'status': 'scored' if windows else 'no_scorable_audio',
            }
            metrics['findings'].sort(key=lambda item: item['start'])
            return {'metrics': metrics, 'quality': score, 'evaluator_version': EVALUATOR_VERSION,
                    'processing': {'elapsed_seconds': round(state['elapsed_seconds'] + time.perf_counter() - started, 3),
                                   'cache_hit': False, 'resume_count': state['resume_count']},
                    'subjective_status': 'requires_human_calibration',
                    'limitations': ['模型分数是人声及背景音质的预测值，不能代替自然度、内容或合成来源判断。',
                                    '背景音乐可能影响背景质量分；异常提示需要回听确认。',
                                    '自动与人工评分独立保存；未输出未经验证的主观自动分。']}
        except EvaluationPaused as error:
            paused = True
            state['elapsed_seconds'] += time.perf_counter() - started
            temporary = folder / 'checkpoint.part.json'
            temporary.write_text(json.dumps(state, ensure_ascii=False))
            temporary.replace(saved_path)
            error.info = self.task_info(state)
            error.progress = 35 + int(60 * state['next_index'] / max(state['total_windows'], 1)) if state['metrics'] else 3
            raise
        finally:
            original.unlink(missing_ok=True)
            mono.unlink(missing_ok=True)
            if not paused:
                saved_path.unlink(missing_ok=True)
            (folder / 'checkpoint.part.json').unlink(missing_ok=True)

    @staticmethod
    def task_info(state):
        return {'processed_windows': state['next_index'], 'scored_windows': len(state['windows']),
                'total_windows': state['total_windows'], 'resume_count': state['resume_count']}
