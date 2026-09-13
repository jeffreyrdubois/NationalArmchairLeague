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

        conn.commit()


def _repair():
    """Put rows the old score sync left inconsistent back into a sane state."""
    from app.services.scoring import repair_pick_scoring

    db = SessionLocal()
    try:
        repair_pick_scoring(db)
    finally:
        db.close()
