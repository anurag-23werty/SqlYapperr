# syntax=docker/dockerfile:1.7
# ---------- build stage: install dependencies into a self-contained venv ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt requirements-otel.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt -r requirements-otel.txt

# ---------- runtime stage: small, non-root, read-only friendly ----------
FROM python:3.12-slim
LABEL org.opencontainers.image.title="SQLYapper" \
      org.opencontainers.image.description="Secure natural-language-to-SQL agent" \
      org.opencontainers.image.source="https://github.com/anurag-23werty/SQLYapper"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQLY_DATA_DIR=/data \
    SQLY_APP_DB_PATH=/data/app.db \
    SQLY_DB_PATH=/data/sample.db \
    SQLY_LOG_FORMAT=json \
    WEB_CONCURRENCY=1
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=app:app app ./app
USER app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]
# One worker per container: metrics live in process memory, so scale by adding containers
# (docker compose up --scale app=3); Prometheus and Caddy discover every replica via DNS.
# --forwarded-allow-ips='*' is safe only because port 8000 is reachable solely from the reverse proxy.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*' --no-access-log --timeout-graceful-shutdown 20"]
