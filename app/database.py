from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/nal.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
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

        # --- users table ---
        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()}
        if "notif_picks_reminder" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN notif_picks_reminder BOOLEAN DEFAULT 1"))
        if "notif_week_results" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN notif_week_results BOOLEAN DEFAULT 1"))

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
