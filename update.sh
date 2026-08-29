#!/bin/bash
#
# Update NAL to the image GitHub Actions published from the latest main.
# Run this from the repo directory on your Unraid host:
#
#     ./update.sh
#
# There is nothing to build: every merge to main publishes a multi-arch image
# to ghcr.io, so an update is a pull and a restart. Works with Docker Compose v2
# ("docker compose"), the standalone "docker-compose" binary, or plain docker if
# no Compose is installed. Your SQLite database (./data) and your .env are left
# untouched.
#
# On Unraid proper you do not need this at all — the container appears in the
# Docker tab with an "update ready" flag, and Apply does the same thing.
set -e

cd "$(dirname "$0")"

IMAGE="ghcr.io/jeffreyrdubois/nationalarmchairleague:latest"

# External (host) port. Defaults to 8000; override with HOST_PORT in .env
# (e.g. HOST_PORT=5950). The container always listens on 8000 internally.
HOST_PORT="$(grep -E '^HOST_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
HOST_PORT="${HOST_PORT:-8000}"

# The repo is still worth keeping current: docker-compose.yml, the Unraid
# template, and this script itself all live in it. A dirty or diverged checkout
# should not stop the container update, though — that is the part that matters.
echo "==> Pulling latest repo files..."
git pull --ff-only || echo "    (skipped — local changes or diverged branch; container update continues)"

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

  echo "==> Pulling the latest image and restarting (using: $COMPOSE)..."
  $COMPOSE pull
  $COMPOSE up -d
else
  echo "==> No Compose found — updating with plain docker..."
  docker pull "$IMAGE"
  docker rm -f nal >/dev/null 2>&1 || true
  docker run -d --name nal --restart unless-stopped \
    -p "${HOST_PORT}:8000" \
    -v "$PWD/data:/app/data" \
    --env-file .env \
    "$IMAGE"
fi

echo "==> Cleaning up old images..."
docker image prune -f >/dev/null 2>&1 || true

# The version the container reports is the only way to tell whether the update
# actually took, which is the whole question you ran this to answer.
echo "==> Waiting for the app to come up..."
for _ in $(seq 1 30); do
  if health="$(curl -fsS "http://127.0.0.1:${HOST_PORT}/health" 2>/dev/null)"; then
    echo "==> Now running: $health"
    exit 0
  fi
  sleep 2
done

echo "==> Container updated, but /health did not answer in 60s — check: docker logs nal"
