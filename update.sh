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

echo "==> Pulling latest code..."
git pull --ff-only

echo "==> Rebuilding and restarting container..."
docker compose up -d --build

echo "==> Cleaning up old images..."
docker image prune -f >/dev/null 2>&1 || true

echo "==> Done. NAL is up to date."
