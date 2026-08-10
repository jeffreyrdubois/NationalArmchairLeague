#!/bin/bash
#
# Pull the latest code and rebuild/restart the NAL container.
# Run this from the repo directory on your Unraid host:
#
#     ./update.sh
#
# Works with Docker Compose v2 ("docker compose"), the standalone
# "docker-compose" binary, or plain docker if no Compose is installed.
# Your SQLite database (./data) and your .env are left untouched.
set -e

cd "$(dirname "$0")"

# External (host) port. Defaults to 8000; override with HOST_PORT in .env
# (e.g. HOST_PORT=5950). The container always listens on 8000 internally.
HOST_PORT="$(grep -E '^HOST_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
HOST_PORT="${HOST_PORT:-8000}"

echo "==> Pulling latest code..."
git pull --ff-only

# Locate a Compose command, if any.
COMPOSE=""
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
else
  for p in docker-compose /usr/local/bin/docker-compose /usr/bin/docker-compose; do
    if command -v "$p" >/dev/null 2>&1; then COMPOSE="$p"; break; fi
  done
fi

if [ -n "$COMPOSE" ]; then
  echo "==> Rebuilding and restarting (using: $COMPOSE)..."
  $COMPOSE up -d --build
else
  echo "==> No Compose found — rebuilding with plain docker..."
  docker build -t nal .
  docker rm -f nal >/dev/null 2>&1 || true
  docker run -d --name nal --restart unless-stopped \
    -p "${HOST_PORT}:8000" \
    -v "$PWD/data:/app/data" \
    --env-file .env \
    nal
fi

echo "==> Cleaning up old images..."
docker image prune -f >/dev/null 2>&1 || true

echo "==> Done. NAL is up to date."
