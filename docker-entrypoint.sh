#!/bin/sh
set -eu

# National Armchair League container entrypoint.
#
# Its whole job is making the first run work without the user doing anything.
# On Unraid the appdata directory is created root-owned by the Docker manager,
# so an app running as a fixed non-root user cannot write to it — the classic
# "it starts, then errors on every save" first-run failure. We follow the
# LinuxServer.io convention Unraid users already expect (PUID/PGID/UMASK,
# defaulting to nobody:users) and fix ownership before dropping privileges.

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
DATA_DIR="${DATA_DIR:-/app/data}"
APP_USER="nal"

log() { printf '[entrypoint] %s\n' "$1"; }

# The login cookie signing key. Left unset, sessions would be signed with the
# placeholder default baked into the source — the same one in every copy of this
# image, so anyone could forge a login as any user. Generating one into the data
# volume instead means a fresh install is secure with no configuration, and the
# key survives updates (it lives in the volume, not the image), so nobody is
# logged out by an update. Setting SECRET_KEY in the environment still wins.
SECRET_FILE="$DATA_DIR/secret.key"
if [ -z "${SECRET_KEY:-}" ]; then
  mkdir -p "$DATA_DIR"
  if [ ! -s "$SECRET_FILE" ]; then
    log "generating a session signing key in $SECRET_FILE"
    python -c "import secrets; print(secrets.token_urlsafe(48))" > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
  fi
  SECRET_KEY="$(cat "$SECRET_FILE")"
  export SECRET_KEY
fi

# If we were started with --user (docker run --user 1000:1000, or a compose
# `user:` key), we cannot change ownership and must not try. Just run.
if [ "$(id -u)" -ne 0 ]; then
  log "running as UID $(id -u) (--user given); skipping PUID/PGID setup"
  umask "$UMASK"
  exec "$@"
fi

# Reuse an existing group with this GID if there is one, otherwise create ours.
# (On Debian, GID 100 is already `users` — the same name Unraid uses.)
if ! existing_group="$(getent group "$PGID" | cut -d: -f1)" || [ -z "$existing_group" ]; then
  groupadd -g "$PGID" "$APP_USER" 2>/dev/null || true
  existing_group="$APP_USER"
fi

# -M: no home directory. -N: do not create a same-named group, we have one.
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$existing_group" -M -N -s /usr/sbin/nologin "$APP_USER" 2>/dev/null \
    || useradd -u "$PUID" -g "$existing_group" -M -N "$APP_USER" 2>/dev/null \
    || true
fi

# The user may already exist with a different UID if PUID changed between runs.
current_uid="$(id -u "$APP_USER" 2>/dev/null || echo '')"
if [ -n "$current_uid" ] && [ "$current_uid" != "$PUID" ]; then
  log "updating $APP_USER UID $current_uid -> $PUID"
  userdel "$APP_USER" 2>/dev/null || true
  useradd -u "$PUID" -g "$existing_group" -M -N -s /usr/sbin/nologin "$APP_USER" 2>/dev/null || true
fi

mkdir -p "$DATA_DIR"

# Only chown when ownership is actually wrong. A recursive chown on every boot
# would churn mtimes for no reason, so the volume is checked first, cheaply,
# and only fixed if something is actually off.
current_owner="$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo 'unknown')"
if [ "$current_owner" != "${PUID}:${PGID}" ]; then
  log "fixing ownership of $DATA_DIR ($current_owner -> ${PUID}:${PGID})"
  chown -R "${PUID}:${PGID}" "$DATA_DIR" || \
    log "WARNING: could not chown $DATA_DIR; writes may fail"
# The directory can be correctly owned while something inside it is not — which
# is exactly what happens when nal.db is copied in by hand over SSH, as root.
# SQLite needs write access even to read (it journals), so that file would fail
# to open with an error that says nothing about permissions.
#
# `-print -quit` stops at the first offender, so this costs one stat in the
# normal case rather than a walk of the whole volume.
elif [ -n "$(find "$DATA_DIR" ! -user "$PUID" -print -quit 2>/dev/null)" ]; then
  log "found files in $DATA_DIR not owned by ${PUID}; fixing ownership"
  chown -R "${PUID}:${PGID}" "$DATA_DIR" || \
    log "WARNING: could not chown $DATA_DIR; writes may fail"
fi

# FastAPI creates the static mount point at import time; it is inside the image
# rather than the volume, so it needs to be writable by the same user.
chown -R "${PUID}:${PGID}" /app/app/static 2>/dev/null || true

umask "$UMASK"
log "starting as ${PUID}:${PGID}"

# exec, so uvicorn receives SIGTERM directly — that is what lets it finish
# in-flight requests and close the database inside Docker's 10s stop window.
exec gosu "${PUID}:${PGID}" "$@"
