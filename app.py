from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
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

from evaluator import Evaluator, EVALUATOR_VERSION, MODEL_SHA256, SCOPE_LABELS, install_model

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
evaluator = Evaluator(MODELS / 'sig_bak_ovr.onnx', threads=MODEL_THREADS)
ALLOWED_SUFFIXES = {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac', '.opus', '.mp4', '.aiff', '.aif'}
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
        result_json TEXT, review_json TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at TEXT NOT NULL
    )''')
    database.execute("UPDATE reviews SET status='uploaded',progress=0,stage='等待重新检测',updated_at=? WHERE status IN ('queued','processing')",
                     (datetime.now(timezone.utc).isoformat(),))

app = FastAPI(title='声检 · 本地音频评测', docs_url=None, redoc_url=None)
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
    response.headers['Cache-Control'] = 'no-store'
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
    allowed = {'status', 'progress', 'stage', 'scope', 'result_json', 'review_json', 'error', 'updated_at'}
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
                result.get('quality', {}).get('model_sha256') == MODEL_SHA256):
            return reused_result(result)
    return None


def job_progress(key, percent, stage):
    with db_lock:
        job = jobs[key]
        job.update(progress=percent, stage=stage)
        update_many(job['ids'], progress=percent, stage=stage)


def run_job(key, row, scope):
    try:
        with db_lock:
            job = jobs[key]
            job.update(started=True, progress=1, stage='读取音频')
            update_many(job['ids'], status='processing', stage='读取音频', progress=1, error=None)
        result = evaluator.evaluate(UPLOADS / row['stored_name'], TEMP / row['id'], scope,
                                    lambda percent, stage: job_progress(key, percent, stage))
        with db_lock:
            for index, identifier in enumerate(jobs[key]['ids']):
                shared = reused_result(result) if index else result
                update(identifier, status='done', progress=100,
                       stage='检测完成 · 复用相同音频' if index else '检测完成',
                       result_json=json.dumps(shared, ensure_ascii=False))
            jobs.pop(key, None)
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else '检测未完成，请确认文件有效后重试。'
        with db_lock:
            update_many(jobs[key]['ids'], status='error', stage='检测失败', progress=0, error=message)
            jobs.pop(key, None)


def queue_status():
    with db_lock:
        return {'workers': WORKERS, 'active_jobs': sum(job['started'] for job in jobs.values()),
                'queued_jobs': sum(not job['started'] for job in jobs.values())}


@app.get('/')
def home():
    return FileResponse(ROOT / 'static' / 'index.html')


@app.get('/api/health')
def health():
    return {'ready': evaluator.ready, 'model': 'DNSMOS P.835 · regular', 'model_sha256': MODEL_SHA256,
            'subjective_model': None, 'local_only': IS_LOCAL, 'max_file_mb': 200, 'max_batch_files': 20,
            'max_run_files': 200, 'workers': WORKERS, 'model_threads': MODEL_THREADS,
            'evaluator_version': EVALUATOR_VERSION, 'scopes': SCOPE_LABELS}


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
    for file in files:
        if Path(file.filename or '').suffix.lower() not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f'不支持这个文件格式：{file.filename or "未命名文件"}')
    added, stored_paths = [], []
    try:
        for file in files:
            identifier = uuid.uuid4().hex
            filename = Path(file.filename or '音频').name[:240]
            stored_name = identifier + Path(filename).suffix.lower()
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
    rows = [get_row(identifier) for identifier in dict.fromkeys(request.ids)]
    submitted, reused = [], []
    with db_lock:
        for row in rows:
            fresh = get_row(row['id'])
            if fresh['status'] in {'queued', 'processing'}:
                continue
            result = None if request.force else cached_result(fresh, request.scope)
            if result:
                update(row['id'], status='done', progress=100, stage='检测完成 · 复用相同音频',
                       scope=request.scope, error=None, result_json=json.dumps(result, ensure_ascii=False))
                reused.append(row['id'])
            else:
                key = (row['sha256'], request.scope, EVALUATOR_VERSION)
                existing = jobs.get(key)
                update(row['id'], status='processing' if existing and existing['started'] else 'queued',
                       progress=existing['progress'] if existing else 0,
                       stage=existing['stage'] if existing else '等待检测', scope=request.scope, error=None)
                if existing:
                    existing['ids'].append(row['id'])
                else:
                    jobs[key] = {'ids': [row['id']], 'started': False, 'progress': 0, 'stage': '等待检测'}
                    executor.submit(run_job, key, row, request.scope)
            submitted.append(row['id'])
    return {'submitted': submitted, 'reused': reused, 'queue': queue_status()}


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


@app.get('/api/audio/{identifier}')
def audio(identifier: str):
    row = get_row(identifier)
    path = UPLOADS / row['stored_name']
    if not path.is_file():
        raise HTTPException(404, '本地音频文件不存在。')
    return FileResponse(path, content_disposition_type='inline')


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
