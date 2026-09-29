from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Literal
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from evaluator import EvaluationPaused, Evaluator, EVALUATOR_VERSION, MODEL_SHA256, SCOPE_LABELS, install_model, read_checkpoint
from playback import CACHE_VERSION, PlaybackCache, PlaybackError, stream_audio
from media import validate_audio

ROOT = Path(__file__).resolve().parent
BIND_HOST = os.environ.get('AUDIO_REVIEW_HOST', '127.0.0.1')
PORT = int(os.environ.get('AUDIO_REVIEW_PORT', '8765'))
if not 1 <= PORT <= 65535:
    raise RuntimeError('AUDIO_REVIEW_PORT 必须在 1–65535 之间。')
ALLOWED_ORIGINS = {f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'}
for value in os.environ.get('AUDIO_REVIEW_ALLOWED_ORIGINS', '').split(','):
    origin = value.strip().rstrip('/')
    if not origin:
        continue
    parsed = urlsplit(origin)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment or '*' in origin):
        raise RuntimeError('AUDIO_REVIEW_ALLOWED_ORIGINS 应为以逗号分隔的完整 http/https 访问地址。')
    ALLOWED_ORIGINS.add(origin)
IS_LOCAL = BIND_HOST in {'127.0.0.1', 'localhost', '::1'}
DEFAULT_DATA = ROOT.parent.parent / 'work' / 'audio-review-runtime' if ROOT.parent.name == 'outputs' else ROOT / 'work'
DATA = Path(os.environ.get('AUDIO_REVIEW_DATA_DIR', DEFAULT_DATA)).resolve()
UPLOADS, MODELS, TEMP = DATA / 'uploads', DATA / 'models', DATA / 'temp'
for directory in (UPLOADS, MODELS, TEMP):
    directory.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / 'reviews.sqlite3'
db_lock = threading.RLock()


def bounded_setting(name, default):
    value = int(os.environ.get(name, default))
    if not 1 <= value <= 8:
        raise RuntimeError(f'{name} 必须在 1–8 之间。')
    return value


WORKERS = bounded_setting('AUDIO_REVIEW_WORKERS', min(2, os.cpu_count() or 1))
MODEL_THREADS = bounded_setting('AUDIO_REVIEW_MODEL_THREADS', 2)
executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='audio-review')
jobs = {}
retired_jobs = {}
pending_cleanup = {}
cleanup_wake = threading.Event()
cleanup_stopped = threading.Event()
stopping = threading.Event()
evaluator = Evaluator(MODELS / 'sig_bak_ovr.onnx', threads=MODEL_THREADS)
MAX_FILE_BYTES = 200 * 1024 * 1024
RATING_KEYS = ['human_likeness', 'naturalness', 'clarity', 'engagement', 'voice_distinction', 'accent_emotion']
LABELS = ['像真人播客', '语音自然度', '音质清晰度', '愿继续听', '声音区分度', '口音情绪合适']


def connect():
    database = sqlite3.connect(DB_PATH, timeout=30)
    database.row_factory = sqlite3.Row
    return database


with connect() as database:
    database.execute('PRAGMA journal_mode=WAL')
    database.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, filename TEXT NOT NULL, stored_name TEXT NOT NULL,
        size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'uploaded', progress INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT '等待检测', scope TEXT NOT NULL DEFAULT 'fast',
        result_json TEXT, review_json TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at TEXT NOT NULL,
        task_json TEXT NOT NULL DEFAULT '{}', resume_result_json TEXT
    )''')
    columns = {row[1] for row in database.execute('PRAGMA table_info(reviews)')}
    if 'task_json' not in columns:
        database.execute("ALTER TABLE reviews ADD COLUMN task_json TEXT NOT NULL DEFAULT '{}'")
    if 'resume_result_json' not in columns:
        database.execute('ALTER TABLE reviews ADD COLUMN resume_result_json TEXT')
    database.execute("UPDATE reviews SET status='uploaded',progress=0,stage='等待重新检测',task_json='{}',resume_result_json=NULL,updated_at=? WHERE status IN ('queued','processing')",
                     (datetime.now(timezone.utc).isoformat(),))
    database.execute("UPDATE reviews SET status='paused',stage='已暂停 · 可继续检测',updated_at=? WHERE status='pausing'",
                     (datetime.now(timezone.utc).isoformat(),))

# A paused checkpoint is compact JSON. Never retain large decoded WAVs after a
# restart; scoring can decode the source again while retaining completed windows.
for folder in TEMP.iterdir():
    if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'[a-f0-9]{32}', folder.name):
        for filename in ('decoded-original.wav', 'decoded-16k.wav', 'checkpoint.part.json'):
            (folder / filename).unlink(missing_ok=True)

@asynccontextmanager
async def lifespan(application):
    try:
        yield
    finally:
        await run_in_threadpool(stop_tasks)
        await run_in_threadpool(playback.close)


app = FastAPI(title='声检 · 本地音频评测', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted({urlsplit(origin).hostname for origin in ALLOWED_ORIGINS}), www_redirect=False)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.middleware('http')
async def local_origin(request: Request, call_next):
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        origin = request.headers.get('origin')
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse({'detail': '当前访问地址未配置为允许来源，请检查 AUDIO_REVIEW_ALLOWED_ORIGINS。'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


def now():
    return datetime.now(timezone.utc).isoformat()


def public_row(row, *, compact=False):
    item = dict(row)
    result_json = item.pop('result_json')
    item['result'] = json.loads(result_json) if result_json else None
    if compact and item['result']:
        item['result']['quality'].pop('windows', None)
        item['result']['metrics'].pop('waveform', None)
    item['review'] = json.loads(item.pop('review_json'))
    item['task'] = json.loads(item.pop('task_json'))
    item.pop('resume_result_json')
    item.pop('stored_name')
    item['sample_id'] = f"S{item['sequence']:02d}"
    return item


def get_row(identifier):
    with connect() as database:
        row = database.execute('SELECT * FROM reviews WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, '找不到这条音频。')
    return row


def update_many(identifiers, **values):
    allowed = {'status', 'progress', 'stage', 'scope', 'result_json', 'review_json', 'error', 'updated_at',
               'task_json', 'resume_result_json'}
    if any(key not in allowed for key in values):
        raise ValueError('Invalid update field')
    values['updated_at'] = now()
    with db_lock, connect() as database:
        database.executemany('UPDATE reviews SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=?',
                             [(*values.values(), identifier) for identifier in identifiers])


def update(identifier, **values):
    update_many([identifier], **values)


def reused_result(result):
    # Human reviews stay on the individual record; only automatic results are shared.
    result = dict(result)
    result['processing'] = {'elapsed_seconds': 0, 'cache_hit': True,
                            'source_elapsed_seconds': result.get('processing', {}).get('source_elapsed_seconds',
                                                         result.get('processing', {}).get('elapsed_seconds'))}
    return result


def cached_result(row, scope):
    with connect() as database:
        candidates = database.execute('''SELECT result_json FROM reviews
            WHERE sha256=? AND scope=? AND status='done' AND result_json IS NOT NULL
            ORDER BY updated_at DESC''', (row['sha256'], scope)).fetchall()
    for candidate in candidates:
        result = json.loads(candidate['result_json'])
        if (result.get('evaluator_version') == EVALUATOR_VERSION and
                result.get('quality', {}).get('model_sha256') == MODEL_SHA256 and
                result.get('quality', {}).get('scope') == scope):
            return reused_result(result)
    return None


def job_key(row, scope=None):
    return (row['sha256'], scope or row['scope'], EVALUATOR_VERSION)


def active_ids(job):
    return [identifier for identifier in job['ids'] if identifier not in job['paused_ids']]


def task_document(row):
    return json.loads(row['task_json'])


def task_info(job, identifier):
    return {'session_id': job['session_id'], 'force': job['force_by_id'][identifier], **job['info']}


def update_task_rows(job, identifiers, **values):
    if not identifiers:
        return
    values['updated_at'] = now()
    keys = [*values, 'task_json']
    with db_lock, connect() as database:
        database.executemany('UPDATE reviews SET ' + ','.join(f'{key}=?' for key in keys) + ' WHERE id=?',
                             [(*values.values(), json.dumps(task_info(job, identifier)), identifier)
                              for identifier in identifiers])


def new_job(row, scope, force, *, restore=False):
    task = task_document(row)
    session_id = task.get('session_id', '') if restore else ''
    if not re.fullmatch(r'[a-f0-9]{32}', session_id):
        session_id = uuid.uuid4().hex
    saved_path = TEMP / session_id / 'checkpoint.json'
    saved = (read_checkpoint(saved_path, scope, row['sha256']) or {}) if restore else {}
    return {'ids': [], 'paused_ids': set(), 'started': False, 'scheduled': False,
            'pause_event': threading.Event(), 'source': UPLOADS / row['stored_name'],
            'session_id': session_id, 'force_by_id': {},
            'progress': row['progress'] if saved else 0,
            'stage': '等待继续检测' if saved else '等待检测',
            'info': {'processed_windows': saved.get('next_index', 0),
                     'scored_windows': len(saved.get('windows', [])),
                     'total_windows': saved.get('total_windows', 0), 'resume_count': saved.get('resume_count', 0)}}


def create_job(row, scope, force, *, restore=False):
    seed = row
    if not restore:
        with connect() as database:
            candidates = database.execute("""SELECT * FROM reviews WHERE sha256=? AND scope=?
                AND status IN ('paused','pausing') AND resume_result_json IS NULL ORDER BY sequence""",
                (row['sha256'], scope)).fetchall()
        for candidate in candidates:
            if re.fullmatch(r'[a-f0-9]{32}', task_document(candidate).get('session_id', '')):
                seed, restore = candidate, True
                break
    job = new_job(seed, scope, force, restore=restore)
    if restore:
        with connect() as database:
            related = database.execute("""SELECT * FROM reviews WHERE sha256=? AND scope=?
                AND status IN ('paused','pausing') AND resume_result_json IS NULL""",
                (row['sha256'], scope)).fetchall()
        for candidate in related:
            task = task_document(candidate)
            if task.get('session_id') == job['session_id']:
                job['ids'].append(candidate['id'])
                job['paused_ids'].add(candidate['id'])
                job['force_by_id'][candidate['id']] = bool(task.get('force'))
    return job


def retire_job(key, job):
    if jobs.get(key) is job:
        jobs.pop(key)
    if job['scheduled']:
        retired_jobs[id(job)] = job


def detach_row(row):
    """Remove one consumer; an exiting worker keeps its reserved slot."""
    key = job_key(row)
    job = jobs.get(key)
    if job is None or row['id'] not in job['ids']:
        return
    job['ids'].remove(row['id'])
    job['paused_ids'].discard(row['id'])
    job['force_by_id'].pop(row['id'], None)
    if job['ids']:
        # A scheduled worker may already have opened the old source. Keep its
        # worker_source reference until finally, while future runs use a survivor.
        if job['source'] == UPLOADS / row['stored_name']:
            survivor = get_row(job['ids'][0])
            job['source'] = UPLOADS / survivor['stored_name']
        if not active_ids(job):
            job['pause_event'].set()
    else:
        job['pause_event'].set()
        retire_job(key, job)
        queue_session_cleanup(job['session_id'])


def safe_original_path(stored_name):
    if not re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]{1,16}', stored_name):
        raise HTTPException(409, '原音频文件路径无效，已停止删除，请检查本地数据。')
    path = UPLOADS / stored_name
    if path.is_symlink() or path.resolve().parent != UPLOADS.resolve() or path.is_dir():
        raise HTTPException(409, '原音频文件是链接或路径无效，已停止删除。')
    return path


def queue_file_cleanup(kind, name, identifier=None):
    pending_cleanup[(kind, name)] = {'kind': kind, 'name': name, 'id': identifier,
                                     'message': '文件仍在使用，稍后自动清理。'}
    cleanup_wake.set()


def queue_session_cleanup(session_id):
    if re.fullmatch(r'[a-f0-9]{32}', session_id):
        queue_file_cleanup('checkpoint', session_id)


def cleanup_owned_files():
    """Retry only named application files; never remove unrelated directories."""
    with db_lock:
        tracked = [*jobs.values(), *retired_jobs.values()]
        with connect() as database:
            rows = database.execute('SELECT stored_name,sha256,task_json FROM reviews').fetchall()
        names = {row['stored_name'] for row in rows}
        digests = {row['sha256'] for row in rows}
        sessions = {task_document(row).get('session_id') for row in rows}
        for token, entry in list(pending_cleanup.items()):
            kind, name = token
            try:
                if kind == 'original':
                    if name in names:
                        pending_cleanup.pop(token, None)
                        continue
                    path = safe_original_path(name)
                    if (any(job['scheduled'] and path in {job['source'], job.get('worker_source')}
                            for job in tracked) or playback.references_source(path)):
                        continue
                    path.unlink(missing_ok=True)
                elif kind == 'checkpoint':
                    if name in sessions or any(job['session_id'] == name and job['ids'] for job in tracked):
                        pending_cleanup.pop(token, None)
                        continue
                    if any(job['session_id'] == name and job['scheduled'] for job in tracked):
                        continue
                    folder = TEMP / name
                    if folder.is_symlink() or folder.resolve().parent != TEMP.resolve():
                        raise RuntimeError('检查点路径无效，无法自动清理。')
                    for filename in ('decoded-original.wav', 'decoded-16k.wav', 'checkpoint.json', 'checkpoint.part.json'):
                        (folder / filename).unlink(missing_ok=True)
                    if folder.exists():
                        folder.rmdir()
                elif kind == 'playback':
                    if name in digests:
                        pending_cleanup.pop(token, None)
                        continue
                    playback.remove(name)
                pending_cleanup.pop(token, None)
            except (OSError, RuntimeError, HTTPException, PlaybackError):
                entry['message'] = '文件清理尚未完成，系统会继续重试；请检查文件权限和磁盘状态。'


def collect_orphan_files():
    # Crash/restart recovery for safe application filenames whose DB rows were
    # already deleted. Preserve every upload and checkpoint still referenced.
    with db_lock, connect() as database:
        rows = database.execute('SELECT stored_name,sha256,task_json FROM reviews').fetchall()
        names = {row['stored_name'] for row in rows}
        sessions = {task_document(row).get('session_id') for row in rows}
        digests = {row['sha256'] for row in rows}
        for path in UPLOADS.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}\.[a-z0-9]{1,16}', path.name) and path.name not in names:
                queue_file_cleanup('original', path.name)
        for folder in TEMP.iterdir():
            if re.fullmatch(r'[a-f0-9]{32}', folder.name) and folder.name not in sessions:
                queue_session_cleanup(folder.name)
        for path in playback.folder.glob(f'*.{CACHE_VERSION}.mp3'):
            digest = path.name.removesuffix(f'.{CACHE_VERSION}.mp3')
            if re.fullmatch(r'[a-f0-9]{64}', digest) and digest not in digests:
                queue_file_cleanup('playback', digest)


def cleanup_loop():
    while not cleanup_stopped.is_set():
        cleanup_wake.wait(timeout=5)
        cleanup_wake.clear()
        if cleanup_stopped.is_set():
            return
        cleanup_owned_files()


def schedule_jobs():
    # This is called under db_lock. At most WORKERS futures exist; paused jobs
    # remain plain metadata and never occupy executor threads or child processes.
    if stopping.is_set():
        return
    reserved = sum(job['scheduled'] for job in [*jobs.values(), *retired_jobs.values()])
    for key, job in jobs.items():
        if reserved >= WORKERS:
            break
        if not job['scheduled'] and active_ids(job):
            job['scheduled'] = True
            job['pause_event'].clear()
            executor.submit(run_job, key, job)
            reserved += 1


def job_progress(key, percent, stage, info=None, *, expected_job=None):
    with db_lock:
        job = expected_job or jobs.get(key)
        if job is None or jobs.get(key) is not job:
            return
        job.update(progress=max(job['progress'], percent), stage=stage)
        if info:
            job['info'].update(info)
        update_task_rows(job, active_ids(job), progress=job['progress'], stage=stage)


def job_checkpoint(key, expected_job=None):
    job = expected_job or jobs.get(key)
    if (stopping.is_set() or job is None or jobs.get(key) is not job or job['pause_event'].is_set()):
        raise EvaluationPaused()


def run_job(key, expected_job=None):
    with db_lock:
        job = expected_job or jobs.get(key)
        if job is None:
            return
        if not active_ids(job) or stopping.is_set():
            job['scheduled'] = False
            retired_jobs.pop(id(job), None)
            if jobs.get(key) is job and not job['ids']:
                jobs.pop(key)
            queue_session_cleanup(job['session_id'])
            cleanup_owned_files()
            schedule_jobs()
            return
        job['started'] = True
        job['worker_source'] = job['source']
        source = job['worker_source']
        update_task_rows(job, active_ids(job), status='processing', stage=job['stage'],
                         progress=job['progress'], error=None)
    try:
        result = evaluator.evaluate(source, TEMP / job['session_id'], key[1],
                                    lambda percent, stage, info=None: job_progress(key, percent, stage, info, expected_job=job),
                                    checkpoint=lambda: job_checkpoint(key, job), source_identity=key[0])
        with db_lock:
            if jobs.get(key) is not job:
                return
            for index, identifier in enumerate(active_ids(job)):
                shared = reused_result(result) if index else result
                update(identifier, status='done', progress=100,
                       stage='检测完成 · 复用相同音频' if index else '检测完成',
                       result_json=json.dumps(shared, ensure_ascii=False), task_json='{}', resume_result_json=None)
            # A paused duplicate does not silently complete or lose its frozen
            # progress. Store the fresh shared result privately for explicit resume.
            for identifier in job['paused_ids']:
                row = get_row(identifier)
                task = task_document(row)
                if row['status'] == 'pausing':
                    task.update(task_info(job, identifier))
                update(identifier, status='paused', stage='已暂停 · 可继续检测' if row['status'] == 'pausing' else row['stage'],
                       task_json=json.dumps(task), resume_result_json=json.dumps(reused_result(result), ensure_ascii=False))
            retire_job(key, job)
            queue_session_cleanup(job['session_id'])
    except EvaluationPaused as error:
        with db_lock:
            if jobs.get(key) is not job:
                return
            if error.info:
                job['info'].update(error.info)
            if error.progress is not None:
                job['progress'] = max(job['progress'], error.progress)
            # Keep the reservation until finally. A concurrent resume must not
            # submit this same job while its previous worker is still exiting.
            for identifier in job['paused_ids']:
                if get_row(identifier)['status'] == 'pausing':
                    update_task_rows(job, [identifier], status='paused', progress=job['progress'], stage='已暂停 · 可继续检测')
            if stopping.is_set():
                update_task_rows(job, active_ids(job), status='queued', progress=job['progress'], stage='服务停止 · 等待重新检测')
            else:
                update_task_rows(job, active_ids(job), status='queued', progress=job['progress'], stage='等待继续检测')
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else '检测未完成，请确认文件有效后重试。'
        with db_lock:
            if jobs.get(key) is not job:
                return
            update_many(active_ids(job), status='error', stage='检测失败', progress=0,
                        error=message, task_json='{}', resume_result_json=None)
            for identifier in job['paused_ids']:
                task = task_document(get_row(identifier))
                task['restart_required'] = True
                update(identifier, status='paused', stage='已暂停 · 继续时重新检测', task_json=json.dumps(task), resume_result_json=None)
            retire_job(key, job)
            queue_session_cleanup(job['session_id'])
    finally:
        with db_lock:
            job.update(started=False, scheduled=False, worker_source=None)
            retired_jobs.pop(id(job), None)
            if jobs.get(key) is job:
                if not job['ids']:
                    jobs.pop(key)
                    queue_session_cleanup(job['session_id'])
            elif not job['ids']:
                queue_session_cleanup(job['session_id'])
            cleanup_owned_files()
            schedule_jobs()


def stop_tasks():
    stopping.set()
    executor.shutdown(wait=True, cancel_futures=True)
    with db_lock:
        cleanup_owned_files()
    cleanup_stopped.set()
    cleanup_wake.set()
    if cleanup_thread.is_alive():
        cleanup_thread.join(timeout=5)


def queue_status():
    with db_lock, connect() as database:
        rows = database.execute("SELECT status,task_json,id FROM reviews WHERE status IN ('paused','pausing')").fetchall()
        paused_sessions = set()
        pausing_sessions = set()
        for row in rows:
            session_id = task_document(row).get('session_id', row['id'])
            if row['status'] == 'pausing':
                pausing_sessions.add(session_id)
            else:
                paused_sessions.add(session_id)
        tracked_jobs = [*jobs.values(), *retired_jobs.values()]
        running_sessions = {job['session_id'] for job in tracked_jobs if job['scheduled']}
        paused_sessions.difference_update(running_sessions)
        return {'workers': WORKERS, 'active_jobs': sum(job['started'] for job in tracked_jobs),
                'queued_jobs': sum(not job['started'] and bool(active_ids(job)) for job in jobs.values()),
                'paused_jobs': len(paused_sessions), 'pausing_jobs': len(pausing_sessions),
                'paused_files': sum(row['status'] == 'paused' for row in rows),
                'pausing_files': sum(row['status'] == 'pausing' for row in rows)}


def evaluation_busy():
    queue = queue_status()
    return bool(queue['active_jobs'] or queue['queued_jobs'])


playback = PlaybackCache(DATA / 'playback', UPLOADS)
collect_orphan_files()
cleanup_owned_files()
cleanup_thread = threading.Thread(target=cleanup_loop, name='audio-file-cleanup', daemon=True)
cleanup_thread.start()


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/health')
def health():
    return {'ready': evaluator.ready, 'model': 'DNSMOS P.835 · regular', 'model_sha256': MODEL_SHA256,
            'subjective_model': None, 'local_only': IS_LOCAL, 'max_file_mb': 200, 'max_batch_files': 20,
            'max_run_files': 200, 'workers': WORKERS, 'model_threads': MODEL_THREADS,
            'evaluator_version': EVALUATOR_VERSION, 'scopes': SCOPE_LABELS, 'task_pause': True,
            'resume_scope': True, 'audio_delete': True}


@app.get('/api/reviews')
def list_reviews(compact: bool = False, detail: str | None = None):
    with connect() as database:
        rows = database.execute('SELECT * FROM reviews ORDER BY sequence').fetchall()
    return {'items': [public_row(row, compact=compact and row['id'] != detail) for row in rows],
            'queue': queue_status()}


@app.get('/api/reviews/{identifier}')
def review_detail(identifier: str):
    return public_row(get_row(identifier))


@app.post('/api/upload')
async def upload(files: list[UploadFile] = File(...)):
    if not 1 <= len(files) <= 20:
        raise HTTPException(400, '每批请选择 1–20 条音频。')
    added, stored_paths = [], []
    try:
        for file in files:
            identifier = uuid.uuid4().hex
            filename = Path(file.filename or '音频').name[:240]
            suffix = Path(filename).suffix.lower()
            stored_name = identifier + (suffix if re.fullmatch(r'\.[a-z0-9]{1,16}', suffix) else '.media')
            destination = UPLOADS / stored_name
            stored_paths.append(destination)
            size, digest = 0, hashlib.sha256()
            with destination.open('wb') as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise HTTPException(413, f'{filename} 超过 200 MB，请先拆分。')
                    output.write(chunk)
                    digest.update(chunk)
            if size == 0:
                raise HTTPException(400, f'{filename} 是空文件。')
            try:
                await run_in_threadpool(validate_audio, destination)
            except RuntimeError as error:
                raise HTTPException(400, f'{filename}：{error}') from error
            added.append((identifier, filename, stored_name, size, digest.hexdigest()))
        with db_lock, connect() as database:
            sequence = database.execute('SELECT COALESCE(MAX(sequence),0) FROM reviews').fetchone()[0]
            for offset, (identifier, filename, stored_name, size, digest) in enumerate(added, 1):
                timestamp = now()
                database.execute('''INSERT INTO reviews
                    (id,sequence,filename,stored_name,size,sha256,created_at,updated_at,scope) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (identifier, sequence + offset, filename, stored_name, size, digest, timestamp, timestamp, 'fast'))
    except Exception:
        for path in stored_paths:
            path.unlink(missing_ok=True)
        raise
    finally:
        for file in files:
            await file.close()
    return {'items': [public_row(get_row(item[0])) for item in added]}


class RunRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    scope: Literal['fast', 'sample', 'full'] = 'fast'
    force: bool = False


@app.post('/api/run')
def run(request: RunRequest):
    if not evaluator.ready:
        raise HTTPException(503, '本地音质模型未就绪，请先运行模型安装。')
    submitted, reused, skipped = [], [], []
    with db_lock:
        rows = [get_row(identifier) for identifier in dict.fromkeys(request.ids)]
        for row in rows:
            fresh = get_row(row['id'])
            if fresh['status'] in {'queued', 'processing', 'paused', 'pausing'}:
                skipped.append(row['id'])
                continue
            result = None if request.force else cached_result(fresh, request.scope)
            if result:
                update(row['id'], status='done', progress=100, stage='检测完成 · 复用相同音频',
                       scope=request.scope, error=None, result_json=json.dumps(result, ensure_ascii=False),
                       task_json='{}', resume_result_json=None)
                reused.append(row['id'])
            else:
                key = job_key(row, request.scope)
                existing = jobs.get(key)
                if existing is None:
                    existing = jobs[key] = create_job(fresh, request.scope, request.force)
                existing['ids'].append(row['id'])
                existing['force_by_id'][row['id']] = request.force
                existing['pause_event'].clear()
                update_task_rows(existing, [row['id']], status='processing' if existing['started'] else 'queued',
                                 progress=existing['progress'], stage=existing['stage'], scope=request.scope,
                                 error=None, resume_result_json=None)
            submitted.append(row['id'])
        schedule_jobs()
    return {'submitted': submitted, 'reused': reused, 'skipped': skipped, 'queue': queue_status()}


class TaskRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


class ResumeRequest(TaskRequest):
    scope: Literal['fast', 'sample', 'full'] | None = None


def saved_resume_result(row, scope):
    if scope != row['scope'] or not row['resume_result_json']:
        return None
    try:
        result = json.loads(row['resume_result_json'])
    except (ValueError, TypeError):
        return None
    quality = result.get('quality') if isinstance(result, dict) else None
    if (not isinstance(quality, dict) or result.get('evaluator_version') != EVALUATOR_VERSION or
            quality.get('model_sha256') != MODEL_SHA256 or quality.get('scope') != scope):
        return None
    return result


@app.post('/api/tasks/pause')
def pause_tasks(request: TaskRequest):
    # Validate every ID before changing any task, including IDs near the end.
    paused, skipped = [], []
    with db_lock:
        rows = [get_row(identifier) for identifier in dict.fromkeys(request.ids)]
        for original in rows:
            row = get_row(original['id'])
            if row['status'] not in {'queued', 'processing'}:
                skipped.append(row['id'])
                continue
            job = jobs.get(job_key(row))
            phase = 'paused'
            if job:
                job['paused_ids'].add(row['id'])
                if not active_ids(job):
                    job['pause_event'].set()
                    if job['started']:
                        phase = 'pausing'
            update(row['id'], status=phase, stage='正在暂停 · 等待当前窗口结束' if phase == 'pausing' else '已暂停 · 可继续检测')
            paused.append(row['id'])
        schedule_jobs()
    return {'paused': paused, 'skipped': skipped, 'queue': queue_status()}


@app.post('/api/tasks/resume')
def resume_tasks(request: ResumeRequest):
    requested_scope = getattr(request, 'scope', None)
    resumed, skipped, scope_changed = [], [], []
    with db_lock:
        rows = [get_row(identifier) for identifier in dict.fromkeys(request.ids)]
        # Validate model availability for the entire batch before detaching rows.
        if not evaluator.ready and any(row['status'] in {'paused', 'pausing'} and
                saved_resume_result(row, requested_scope or row['scope']) is None
                for row in rows):
            raise HTTPException(503, '本地音质模型未就绪，请先运行模型安装。')
        for original in rows:
            row = get_row(original['id'])
            if row['status'] not in {'paused', 'pausing'}:
                skipped.append(row['id'])
                continue
            task = task_document(row)
            force = bool(task.get('force'))
            scope = requested_scope or row['scope']
            changed = scope != row['scope']
            if changed:
                detach_row(row)
                queue_session_cleanup(task.get('session_id', ''))
                scope_changed.append(row['id'])
            result = saved_resume_result(row, scope)
            key = job_key(row, scope)
            job = jobs.get(key)
            if result is None and job is None and not force:
                result = cached_result(row, scope)
            if result:
                update(row['id'], status='done', progress=100, stage='检测完成 · 复用相同音频', error=None,
                       scope=scope, result_json=json.dumps(reused_result(result), ensure_ascii=False), task_json='{}', resume_result_json=None)
            else:
                if job is None:
                    # New scope starts from scratch. It may join an existing job
                    # of exactly that scope, but cannot restore the old session.
                    job = jobs[key] = (new_job(row, scope, force) if changed else create_job(row, scope, force, restore=True))
                if row['id'] not in job['ids']:
                    job['ids'].append(row['id'])
                job['paused_ids'].discard(row['id'])
                job['force_by_id'][row['id']] = force
                job['pause_event'].clear()
                update_task_rows(job, [row['id']], status='processing' if job['started'] else 'queued',
                                 scope=scope, progress=job['progress'], stage=job['stage'] if job['started'] else
                                 '评测方式已更改 · 等待重新检测' if changed else '等待继续检测',
                                 error=None, resume_result_json=None)
            resumed.append(row['id'])
        cleanup_owned_files()
        schedule_jobs()
    return {'resumed': resumed, 'skipped': skipped, 'scope_changed': scope_changed, 'queue': queue_status()}


def delete_reviews(request: TaskRequest):
    deleted, cache_keys_removed, retained_digests = [], [], []
    cleanup_tokens = set()
    with db_lock:
        # Unknown IDs or unsafe source paths reject the whole batch before writes.
        rows = [get_row(identifier) for identifier in dict.fromkeys(request.ids)]
        for row in rows:
            safe_original_path(row['stored_name'])
            playback.cache_key(row['sha256'])
        for row in rows:
            detach_row(row)
            session_id = task_document(row).get('session_id', '')
            queue_session_cleanup(session_id)
            cleanup_tokens.add(('checkpoint', session_id))
            queue_file_cleanup('original', row['stored_name'], row['id'])
            cleanup_tokens.add(('original', row['stored_name']))
        with connect() as database:
            database.executemany('DELETE FROM reviews WHERE id=?', [(row['id'],) for row in rows])
            remaining = {row['sha256'] for row in database.execute('SELECT DISTINCT sha256 FROM reviews')}
        for digest in dict.fromkeys(row['sha256'] for row in rows):
            if digest in remaining:
                retained_digests.append(digest)
            else:
                cache_keys_removed.append(playback.cache_key(digest))
                queue_file_cleanup('playback', digest)
                cleanup_tokens.add(('playback', digest))
        cleanup_owned_files()
        schedule_jobs()
        deleted = [row['id'] for row in rows]
        cleanup = [dict(entry) for token, entry in pending_cleanup.items() if token in cleanup_tokens]
    return {'deleted': deleted, 'cache_keys_removed': cache_keys_removed,
            'retained_digests': retained_digests, 'cleanup_pending': cleanup, 'queue': queue_status()}


@app.delete('/api/reviews/{identifier}')
def delete_review(identifier: str):
    return delete_reviews(TaskRequest(ids=[identifier]))


@app.post('/api/reviews/delete')
def delete_review_batch(request: TaskRequest):
    return delete_reviews(request)


class HumanReview(BaseModel):
    ratings: dict[str, int | None] = Field(default_factory=dict)
    ai_suspicion: Literal['', '怀疑', '不确定', '不怀疑'] = ''
    reviewer: str = Field(default='', max_length=80)
    notes: str = Field(default='', max_length=5000)

    @field_validator('ratings')
    @classmethod
    def valid_ratings(cls, value):
        if set(value) - set(RATING_KEYS) or any(x is not None and not 1 <= x <= 5 for x in value.values()):
            raise ValueError('评分应为 1–5 或不适用。')
        return value


@app.put('/api/reviews/{identifier}/human')
def save_review(identifier: str, review: HumanReview):
    get_row(identifier)
    document = review.model_dump()
    document['saved_at'] = now()
    document['complete'] = all(key in document['ratings'] for key in RATING_KEYS) and bool(review.ai_suspicion)
    update(identifier, review_json=json.dumps(document, ensure_ascii=False))
    return public_row(get_row(identifier))


@app.api_route('/api/audio/{identifier}', methods=['GET', 'HEAD'])
def audio(identifier: str, request: Request):
    row = get_row(identifier)
    path = UPLOADS / row['stored_name']
    if not path.is_file():
        raise HTTPException(404, '本地音频文件不存在。')
    return stream_audio(path, request)


def playback_status(row):
    status = playback.status(row['sha256'])
    status['stream_url'] = f"/api/playback/{row['id']}/audio?key={status['cache_key']}" if status['state'] == 'ready' else None
    return status


@app.post('/api/playback/{identifier}/prepare')
def prepare_playback(identifier: str):
    with db_lock:
        row = get_row(identifier)
        result = json.loads(row['result_json']) if row['result_json'] else {}
        duration = result.get('metrics', {}).get('duration')
        try:
            playback.prepare(UPLOADS / row['stored_name'], row['sha256'], duration)
        except PlaybackError as error:
            return JSONResponse({'state': 'error', 'progress': None, 'message': str(error),
                                 'prepared_seconds': 0, 'format': 'mp3', 'stream_url': None,
                                 'cache_key': playback.cache_key(row['sha256']), 'bytes': None}, status_code=409)
        return playback_status(row)


@app.get('/api/playback/{identifier}/status')
def get_playback_status(identifier: str):
    return playback_status(get_row(identifier))


@app.api_route('/api/playback/{identifier}/audio', methods=['GET', 'HEAD'])
def compatible_audio(identifier: str, request: Request, key: str | None = None):
    row = get_row(identifier)
    if key is not None and key != playback.cache_key(row['sha256']):
        raise HTTPException(404, '回听缓存标识不匹配，请刷新后重试。')
    return playback.audio_response(row['sha256'], request, filename=f"{row['id']}.mp3")


def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


@app.get('/api/export.csv')
def export_csv(blind: bool = False):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['样本', '音频文件', '检测状态', '时长(秒)', '自动整体音质(1–5)', '自动人声音质',
                     '自动背景质量', '音质评分范围', '抽样时长(秒)', '人工像真人播客', '人工语音自然度',
                     '人工音质清晰度', '人工愿继续听', '人工声音区分度', '人工口音情绪合适',
                     '人工是否怀疑AI合成', '判为AI的理由及备注', '评分人', '人工评分状态',
                     '问题片段', '模型版本', '原始文件SHA256', '评分窗口步长(秒)', '本次计算耗时(秒)', '复用自动结果'])
    for item in list_reviews()['items']:
        result = item['result'] or {}
        quality, metrics, review = result.get('quality', {}), result.get('metrics', {}), item['review']
        ratings = review.get('ratings', {})
        findings = '；'.join(f"{f['start']:.1f}–{f['end']:.1f}s {f['title']}" for f in metrics.get('findings', []))
        cells = [item['sample_id'], item['sample_id'] if blind else item['filename'], item['status'],
                 metrics.get('duration', ''), quality.get('overall', ''), quality.get('speech', ''), quality.get('background', ''),
                 ('抽样精细' if quality.get('scope') == 'sample' and quality.get('target_hop_seconds', quality.get('hop_seconds')) == 1
                  else SCOPE_LABELS.get(quality.get('scope'), '')), quality.get('coverage_seconds', ''),
                 *[ratings.get(key, '') if ratings.get(key, '') is not None else '不适用' for key in RATING_KEYS],
                 review.get('ai_suspicion', ''), review.get('notes', ''), review.get('reviewer', ''),
                 '已完成' if review.get('complete') else '部分评分' if review else '未评分',
                 findings, quality.get('model_sha256', ''), item['sha256'], quality.get('hop_seconds', ''),
                 result.get('processing', {}).get('elapsed_seconds', ''),
                 '是' if result.get('processing', {}).get('cache_hit') else '否' if result else '']
        writer.writerow([safe_cell(value) for value in cells])
    content = '\ufeff' + output.getvalue()
    return Response(content, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="audio-review.csv"'})


@app.get('/api/export.json')
def export_json():
    return JSONResponse(list_reviews(), headers={'Content-Disposition': 'attachment; filename="audio-review.json"'})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-model', action='store_true')
    args = parser.parse_args()
    if args.install_model:
        path = install_model(evaluator.model_path)
        print('DNSMOS 已安装并通过 SHA256 校验：', path)
    else:
        import uvicorn
        print(f'音频评测：http://{BIND_HOST}:{PORT}', flush=True)
        uvicorn.run(app, host=BIND_HOST, port=PORT, workers=1, log_level='warning')
