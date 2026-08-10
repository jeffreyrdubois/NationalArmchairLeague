#!/bin/bash
#
# Pull the latest code and rebuild/restart the NAL container.
# Run this from the repo directory on your Unraid host:
#
#     ./update.sh
#
# Your SQLite database (./data) and your .env are left untouched.
set -e

cd "$(dirname "$0")"

# Support both the Compose v2 plugin ("docker compose") and the older
# standalone binary ("docker-compose", common on Unraid).
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "Error: neither 'docker compose' nor 'docker-compose' is available." >&2
  echo "Install the Compose plugin or the docker-compose binary and retry." >&2
  exit 1
fi

echo "==> Pulling latest code..."
git pull --ff-only

echo "==> Rebuilding and restarting container (using: $DC)..."
$DC up -d --build

echo "==> Cleaning up old images..."
docker image prune -f >/dev/null 2>&1 || true

echo "==> Done. NAL is up to date."
