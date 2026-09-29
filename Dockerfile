FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
    AUDIO_REVIEW_HOST=0.0.0.0 \
    AUDIO_REVIEW_PORT=8001 \
    AUDIO_REVIEW_WORKERS=2 \
    AUDIO_REVIEW_MODEL_THREADS=2 \
    AUDIO_REVIEW_DATA_DIR=/data

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 audio-review \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin audio-review \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY requirements.lock.txt ./
RUN python -m pip install --no-cache-dir -r requirements.lock.txt

COPY --chown=10001:10001 . /app
RUN chmod +x /app/docker-entrypoint.sh
USER 10001:10001

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import json,os,sys; from urllib.request import build_opener,ProxyHandler; r=build_opener(ProxyHandler({})).open('http://127.0.0.1:'+os.environ['AUDIO_REVIEW_PORT']+'/api/health',timeout=3); sys.exit(0 if json.load(r)['ready'] else 1)"
ENTRYPOINT ["/app/docker-entrypoint.sh"]
