from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str:
    """Pick the database URL, never leaving an empty env override in place.

    Compose and Unraid both pass ``DATABASE_URL`` through even when blank.
    ``os.getenv("DATABASE_URL", default)`` returns ``""`` in that case, which
    would hand SQLAlchemy an unusable URL and surface as the deeply unhelpful
    ``unable to open database file`` once anything tries to write.
    """
    configured = (os.getenv("DATABASE_URL") or "").strip()
    if configured:
        return configured
    data_dir = Path(os.getenv("DATA_DIR") or "./data")
    return f"sqlite:///{(data_dir.resolve() / 'nal.db')}"


DATABASE_URL = _resolve_database_url()


def _sqlite_file_path(url_str: str) -> Path | None:
    """Filesystem path for a file-backed SQLite URL, else None."""
    url = make_url(url_str)
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    path = Path(database)
    return path if path.is_absolute() else Path.cwd() / path


def _build_engine(url_str: str):
    """Create the engine, hardening the SQLite path Unraid deployments hit.

    After a container or array restart the classic failure mode is: SELECTs
    still work (an open FD or a readable ``nal.db``), but the first UPDATE
    dies with ``unable to open database file`` because SQLite cannot create
    its journal next to the database. Absolute paths, an ensured parent
    directory, a busy timeout, and ``NullPool`` (no reused FDs from before a
    remount) make that recoverable instead of a silent every-30s warning.
    """
    url = make_url(url_str)
    connect_args: dict = {}
    engine_kwargs: dict = {}

    if url.get_backend_name() == "sqlite":
        path = _sqlite_file_path(url_str)
        connect_args = {"check_same_thread": False, "timeout": 30}
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            url = url.set(database=str(path.resolve()))
            # Fresh connection per checkout: a pooled FD survives a volume
            # remount looking healthy until the first journal create, which is
            # exactly the "reads work, score sync status cannot write" shape.
            # Not used for :memory: — that needs a single shared connection.
            engine_kwargs["poolclass"] = NullPool

    return create_engine(url, connect_args=connect_args, **engine_kwargs)


engine = _build_engine(DATABASE_URL)

if make_url(str(engine.url)).get_backend_name() == "sqlite":

    @event.listens_for(engine, "connect")
    def _on_sqlite_connect(dbapi_conn, _connection_record):
        # busy_timeout is milliseconds; complements the connect() timeout so a
        # brief FUSE stall waits instead of failing the score-sync write.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_sqlite_writable() -> bool:
    """Return whether the volume will accept a SQLite write.

    Logs loudly on failure but does **not** raise: taking the whole league
    offline on kickoff night because Unraid's appdata ownership glitched is
    worse than serving reads while writes warn. The entrypoint already printed
    the same diagnosis; this is the in-process confirmation.
    """
    path = _sqlite_file_path(str(engine.url))
    if path is None:
        return True

    probe = path.parent / ".nal_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as e:
        logger.error(
            "Data directory %s is not writable (%s). SQLite needs to create "
            "journal files beside the database. On Unraid: chown -R 99:100 "
            "the appdata folder (or your PUID:PGID) and prefer a cache-pool "
            "mapping over /mnt/user.",
            path.parent, e,
        )
        return False

    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS _nal_write_probe (x INTEGER)"))
            conn.execute(text("INSERT INTO _nal_write_probe (x) VALUES (1)"))
            conn.execute(text("DELETE FROM _nal_write_probe"))
            conn.execute(text("DROP TABLE _nal_write_probe"))
    except Exception as e:  # noqa: BLE001 - startup must continue for reads
        logger.error(
            "SQLite write probe failed at %s: %s. Serving reads; writes may fail "
            "until the data volume is writable by PUID:PGID.",
            path, e,
        )
        return False
    return True


# Kept as an alias so older tests/imports keep working.
_assert_sqlite_writable = check_sqlite_writable


def init_db():
    from app import models  # noqa: F401
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:  # noqa: BLE001
        logger.error("Could not ensure database schema: %s", e)
        # A readable existing DB is still worth serving; a total open failure
        # will surface on the first request.
        return
    if not check_sqlite_writable():
        logger.error(
            "Skipping migrate/repair until the data volume is writable — "
            "the app is up for reads."
        )
        return
    _migrate()
    _repair()


def _migrate():
    """Apply incremental schema changes that create_all won't handle on existing tables."""
    with engine.connect() as conn:
        # --- weeks table ---
        week_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(weeks)")).fetchall()}
        if "picks_lock_override" not in week_cols:
            conn.execute(text("ALTER TABLE weeks ADD COLUMN picks_lock_override BOOLEAN DEFAULT 0"))
        if "picks_reminder_sent" not in week_cols:
            conn.execute(text("ALTER TABLE weeks ADD COLUMN picks_reminder_sent BOOLEAN DEFAULT 0"))
        if "picks_webhook_sent" not in week_cols:
            conn.execute(text("ALTER TABLE weeks ADD COLUMN picks_webhook_sent BOOLEAN DEFAULT 0"))

        # --- users table ---
        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        if "notif_picks_reminder" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN notif_picks_reminder BOOLEAN DEFAULT 1"))
        if "notif_week_results" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN notif_week_results BOOLEAN DEFAULT 1"))
        # How a player wants prize money sent. Null until they pick one, so
        # existing accounts simply read "not set" rather than a guessed rail.
        if "preferred_payment" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN preferred_payment VARCHAR(20)"))

        # --- games table ---
        game_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(games)")).fetchall()}
        if game_cols and "score_source" not in game_cols:
            # Existing scores came from the feed; anything typed by hand from
            # here on is re-flagged as manual when it is saved.
            conn.execute(text(
                "ALTER TABLE games ADD COLUMN score_source VARCHAR(10) DEFAULT 'api'"
            ))
        if game_cols and "score_updated_at" not in game_cols:
            conn.execute(text("ALTER TABLE games ADD COLUMN score_updated_at DATETIME"))

        # --- playoff_teams table ---
        # Existing rows predate the 3-state model and all represent clinched
        # teams, so backfill the new status column with 'clinched'.
        playoff_cols = {
            row[1] for row in conn.execute(text("PRAGMA table_info(playoff_teams)")).fetchall()
        }
        if playoff_cols and "status" not in playoff_cols:
            conn.execute(text(
                "ALTER TABLE playoff_teams ADD COLUMN status VARCHAR(20) "
                "NOT NULL DEFAULT 'clinched'"
            ))

        # --- oauth_clients table ---
        # Self-registered clients have no secret and no admin behind them, so
        # both columns became nullable. SQLite can't drop a NOT NULL in place:
        # rebuild the table (its client_id is what oauth_tokens points at, and
        # it is copied unchanged).
        client_info = conn.execute(text("PRAGMA table_info(oauth_clients)")).fetchall()
        client_cols = {row[1]: row for row in client_info}
        if client_cols and "self_registered" not in client_cols:
            conn.execute(text("""
                CREATE TABLE oauth_clients_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    client_id VARCHAR(64) NOT NULL,
                    secret_hash VARCHAR(64),
                    name VARCHAR(100) NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    created_by_id INTEGER REFERENCES users (id),
                    self_registered BOOLEAN NOT NULL DEFAULT 0,
                    created_at DATETIME DEFAULT (CURRENT_TIMESTAMP),
                    last_used_at DATETIME
                )
            """))
            conn.execute(text("""
                INSERT INTO oauth_clients_new
                    (id, client_id, secret_hash, name, redirect_uris,
                     created_by_id, self_registered, created_at, last_used_at)
                SELECT id, client_id, secret_hash, name, redirect_uris,
                       created_by_id, 0, created_at, last_used_at
                FROM oauth_clients
            """))
            conn.execute(text("DROP TABLE oauth_clients"))
            conn.execute(text("ALTER TABLE oauth_clients_new RENAME TO oauth_clients"))
            conn.execute(text(
                "CREATE UNIQUE INDEX ix_oauth_clients_client_id ON oauth_clients (client_id)"
            ))
            conn.execute(text("CREATE INDEX ix_oauth_clients_id ON oauth_clients (id)"))

        conn.commit()


def _repair():
    """Put rows the old score sync left inconsistent back into a sane state."""
    from app.services.scoring import repair_pick_scoring

    db = SessionLocal()
    try:
        repair_pick_scoring(db)
    finally:
        db.close()
