from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    ForeignKey, UniqueConstraint, Enum as SAEnum, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from app.database import Base


class Role(str, enum.Enum):
    player = "player"
    contributor = "contributor"
    admin = "admin"


class SpreadSource(str, enum.Enum):
    api = "api"
    manual = "manual"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String(50), nullable=False)
    last_name = Column(String(50), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    role = Column(SAEnum(Role), default=Role.player, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    picks = relationship("Pick", back_populates="user")

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"


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

    # Spread: positive = home team favored by X; negative = away team favored by abs(X)
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
