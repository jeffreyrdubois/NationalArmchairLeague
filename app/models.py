from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    ForeignKey, UniqueConstraint, Enum as SAEnum, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime
import enum
import secrets
from app.database import Base


class Role(str, enum.Enum):
    player = "player"
    contributor = "contributor"
    admin = "admin"


class SpreadSource(str, enum.Enum):
    api = "api"
    manual = "manual"


class TeamPlayoffStatus(str, enum.Enum):
    """A team's playoff standing for a season.

    A team with no playoff_teams row is treated as "in the hunt" — neither
    clinched nor eliminated. Only teams explicitly marked ``eliminated`` count
    toward the Bottom Feeder award.
    """
    clinched = "clinched"
    eliminated = "eliminated"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(SAEnum(Role), default=Role.player, nullable=False)
    is_active = Column(Boolean, default=True)
    # Notification preferences (all on by default)
    notif_picks_reminder = Column(Boolean, default=True)   # 2 hrs before picks lock
    notif_week_results = Column(Boolean, default=True)     # when week is scored
    created_at = Column(DateTime, server_default=func.now())

    picks = relationship("Pick", back_populates="user")
    push_subscriptions = relationship("PushSubscription", back_populates="user", cascade="all, delete-orphan")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


# Unambiguous alphabet — no O/0, I/1, etc. so codes survive being read aloud
# or typed off a screenshot.
_INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_invite_code() -> str:
    """A 12-character code formatted in dash-separated groups (XXXX-XXXX-XXXX)."""
    raw = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def normalize_invite_code(code: str) -> str:
    """Canonicalize user-entered codes: upper-cased, whitespace stripped."""
    return "".join((code or "").split()).upper()


class Invite(Base):
    """A single-use registration invite.

    Registration is invite-only: a code must be redeemed to create an account
    (the sole exception is the very first account on a fresh install, which
    bootstraps the admin).  An invite may optionally be locked to one email
    address and/or given an expiry.
    """
    __tablename__ = "invites"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    # When set, only this email address may redeem the invite.
    email = Column(String(255))
    note = Column(String(255))
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime)          # naive UTC; None = never expires
    revoked_at = Column(DateTime)
    used_by_id = Column(Integer, ForeignKey("users.id"))
    used_at = Column(DateTime)

    created_by = relationship("User", foreign_keys=[created_by_id])
    used_by = relationship("User", foreign_keys=[used_by_id])

    @property
    def is_used(self) -> bool:
        return self.used_by_id is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.utcnow()

    @property
    def is_valid(self) -> bool:
        return not (self.is_used or self.is_revoked or self.is_expired)

    @property
    def status(self) -> str:
        if self.is_used:
            return "used"
        if self.is_revoked:
            return "revoked"
        if self.is_expired:
            return "expired"
        return "active"


class Season(Base):
    __tablename__ = "seasons"

    id = Column(Integer, primary_key=True, index=True)
    year = Column(Integer, unique=True, nullable=False)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

    weeks = relationship("Week", back_populates="season")


class Week(Base):
    __tablename__ = "weeks"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    week_number = Column(Integer, nullable=False)  # 1-18 regular season, 19+ playoffs
    label = Column(String(50))  # e.g. "Week 1", "Wild Card", "Super Bowl"
    first_kickoff = Column(DateTime)          # when picks lock
    spread_lock_time = Column(DateTime)       # 24h before first kickoff
    is_picks_locked = Column(Boolean, default=False)
    picks_lock_override = Column(Boolean, default=False)  # admin manually unlocked; skip auto-relock
    picks_reminder_sent = Column(Boolean, default=False)  # push notification sent for this week
    is_spreads_locked = Column(Boolean, default=False)
    is_completed = Column(Boolean, default=False)
    espn_week = Column(Integer)               # ESPN API week number
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("season_id", "week_number"),)

    season = relationship("Season", back_populates="weeks")
    games = relationship("Game", back_populates="week")


class Game(Base):
    __tablename__ = "games"

    id = Column(Integer, primary_key=True, index=True)
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False)
    espn_game_id = Column(String(50), unique=True, index=True)
    home_team = Column(String(10), nullable=False)   # abbreviation e.g. "NE"
    away_team = Column(String(10), nullable=False)
    home_team_name = Column(String(100))              # full name
    away_team_name = Column(String(100))
    home_team_logo = Column(String(500))
    away_team_logo = Column(String(500))
    kickoff_time = Column(DateTime)

    # Spread: negative = home team favored by abs(X); positive = away team favored by X
    # We store the spread from home team's perspective after rounding to nearest 0.5
    spread = Column(Float)                    # e.g. -3.5 means home is favored by 3.5
    spread_source = Column(SAEnum(SpreadSource), default=SpreadSource.api)
    spread_override_by = Column(Integer, ForeignKey("users.id"))
    spread_updated_at = Column(DateTime)

    # Scores
    home_score = Column(Integer)
    away_score = Column(Integer)
    is_final = Column(Boolean, default=False)
    is_in_progress = Column(Boolean, default=False)
    quarter = Column(String(10))
    time_remaining = Column(String(20))

    # Derived: who covered the spread
    # home_covered = True means home team won by more than the spread
    home_covered = Column(Boolean)           # None until final

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    week = relationship("Week", back_populates="games")
    picks = relationship("Pick", back_populates="game")
    spread_override_user = relationship("User", foreign_keys=[spread_override_by])


class Pick(Base):
    __tablename__ = "picks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    game_id = Column(Integer, ForeignKey("games.id"), nullable=False)
    week_id = Column(Integer, ForeignKey("weeks.id"), nullable=False)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)

    # Which team they picked (team abbreviation)
    picked_team = Column(String(10), nullable=False)

    # Points wagered (1 to N where N = number of games that week)
    confidence_points = Column(Integer, nullable=False)

    # Scoring
    is_correct = Column(Boolean)     # None until game is final
    points_earned = Column(Float)    # confidence_points if correct, 0 if not, None if pending

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "game_id"),
        UniqueConstraint("user_id", "week_id", "confidence_points"),
    )

    user = relationship("User", back_populates="picks")
    game = relationship("Game", back_populates="picks")


class AuditLog(Base):
    """Track admin/contributor changes for accountability."""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(100), nullable=False)
    target_type = Column(String(50))   # "pick", "game", "spread", "user"
    target_id = Column(Integer)
    detail = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User")


class PushSubscription(Base):
    """Browser Web Push subscriptions — one row per browser/device."""
    __tablename__ = "push_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    endpoint = Column(Text, unique=True, nullable=False)
    p256dh = Column(Text, nullable=False)    # browser public key
    auth_key = Column(Text, nullable=False)  # browser auth secret
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="push_subscriptions")


class AppSetting(Base):
    """Generic key/value store for app-wide configuration (e.g. VAPID keys)."""
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)


class PlayoffTeam(Base):
    """A team's playoff standing for a season (used by the Bottom Feeder award).

    Despite the historical table name, a row here records that a team has
    *reached a definitive state* — either ``clinched`` a playoff spot or been
    ``eliminated``. Teams with no row are still "in the hunt" and count for
    neither. Only ``eliminated`` teams feed the Bottom Feeder award.
    """
    __tablename__ = "playoff_teams"

    id = Column(Integer, primary_key=True, index=True)
    season_id = Column(Integer, ForeignKey("seasons.id"), nullable=False)
    team_abbreviation = Column(String(10), nullable=False)
    status = Column(
        SAEnum(TeamPlayoffStatus),
        default=TeamPlayoffStatus.clinched,
        nullable=False,
    )

    __table_args__ = (UniqueConstraint("season_id", "team_abbreviation"),)

    season = relationship("Season")


class Transaction(Base):
    """League fund transactions — entry fee payments in and prize payouts out."""
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)           # always positive
    direction = Column(String(3), nullable=False)    # 'in' = player paid, 'out' = player received
    note = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())
    logged_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user = relationship("User", foreign_keys=[user_id])
    logged_by = relationship("User", foreign_keys=[logged_by_id])
