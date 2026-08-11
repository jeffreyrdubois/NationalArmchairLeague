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
  # A container literally named "nal" that this Compose project does NOT manage
  # (e.g. one first created by an older plain-docker run or the Unraid Docker
  # template) blocks Compose from reusing the name and aborts the update with:
  #   the container name "/nal" is already in use by container "..."
  # Compose won't touch a container it didn't create, so detect that case — a
  # "nal" container exists, but Compose doesn't consider it part of this
  # project — and remove the stray one. The app is stateless (all data lives in
  # ./data), so Compose simply recreates it below.
  if [ -n "$(docker ps -aqf 'name=^/nal$' 2>/dev/null)" ] \
     && [ -z "$($COMPOSE ps -q nal 2>/dev/null)" ]; then
    echo "==> Removing stray 'nal' container not managed by Compose..."
    docker rm -f nal >/dev/null 2>&1 || true
  fi

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
