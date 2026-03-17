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

        # --- playoff_teams table (created by create_all; no ALTER needed) ---
        # create_all handles new tables automatically; this comment anchors future migrations.

        conn.commit()
