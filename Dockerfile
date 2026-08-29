# National Armchair League — one container, no external services.
#
# Debian slim rather than Alpine: every dependency here (cryptography, bcrypt,
# uvloop, httptools) ships manylinux wheels for both amd64 and arm64, so the
# image builds in seconds on each architecture instead of compiling C on an
# emulated arm64 runner. Not distroless: Unraid's container "Console" button
# needs a shell, and PUID/PGID support needs a root phase before dropping
# privileges.

# ---------------------------------------------------------------------------
# Stage 1: dependencies (anything that needs a compiler compiles here)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

# Kept so a missing wheel degrades to a slower build rather than a failed one.
# In practice nothing compiles: all four native dependencies resolve to wheels
# on both published architectures.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the system site-packages, so the runtime stage can
# take the whole dependency tree with a single COPY and nothing else.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# gosu: drop privileges in the entrypoint. tini: reap zombies — APScheduler
# runs background jobs, so PID 1 needs to behave like an init.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu tini \
 && rm -rf /var/lib/apt/lists/* \
 && gosu nobody true \
 && test -x /usr/bin/tini

COPY --from=deps /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    DATA_DIR=/app/data \
    DATABASE_URL=sqlite:////app/data/nal.db \
    PUID=99 \
    PGID=100 \
    UMASK=022

WORKDIR /app

COPY app/ ./app/
COPY healthcheck.py ./healthcheck.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /app/data /app/app/static/css /app/app/static/js

VOLUME ["/app/data"]
EXPOSE 8000

# 60s interval, and the probe neither writes nor logs: frequent chatty
# healthchecks keep Unraid array drives awake and churn docker.img, which is a
# long-standing community complaint.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python /app/healthcheck.py

# Build metadata, surfaced at /health and in the page footer so you can confirm
# what you are actually running after an update. The publish workflow passes a
# real version; the default is deliberately marked -dev so an image built by
# hand cannot be mistaken for a released one on the very screen that exists to
# tell you what you are running.
ARG APP_VERSION=0.0.0-dev
ARG BUILD_DATE
ARG GIT_COMMIT
ENV APP_VERSION=${APP_VERSION} \
    BUILD_DATE=${BUILD_DATE} \
    GIT_COMMIT=${GIT_COMMIT}

LABEL org.opencontainers.image.title="National Armchair League" \
      org.opencontainers.image.description="Family NFL confidence-pick league." \
      org.opencontainers.image.source="https://github.com/jeffreyrdubois/NationalArmchairLeague" \
      org.opencontainers.image.version="${APP_VERSION}"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
