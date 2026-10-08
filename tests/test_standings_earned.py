"""The season leaderboard shows prize money each player has earned.

Players asked for this on the standings page, between total points and correct.
The figure is the same one the funds page calls earned: weekly prizes once a
week is final, and nothing projected for the season or the awards until the
season is over.

Run with: python tests/test_standings_earned.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-earned-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, PayoutPlan, PayoutRule, Role, Season, User, Week
from app.routers import dashboard

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(dashboard.router)


def build():
    db = SessionLocal()
    season = Season(year=2026, is_active=True)
    db.add(season)
    db.flush()
    week = Week(
        season_id=season.id,
        week_number=1,
        label="Week 1",
        is_completed=True,
        is_picks_locked=True,
        first_kickoff=datetime.utcnow() - timedelta(days=3),
    )
    db.add(week)
    db.flush()
    game = Game(
        week_id=week.id,
        home_team="KC",
        away_team="BUF",
        kickoff_time=datetime.utcnow() - timedelta(days=2),
        spread=-3.0,
        is_final=True,
        home_score=24,
        away_score=17,
    )
    db.add(game)
    db.flush()
    winner = User(
        first_name="Ada", last_name="Admin", email="ada@x.com",
        password_hash="x", role=Role.player,
    )
    other = User(
        first_name="Bea", last_name="Bee", email="bea@x.com",
        password_hash="x", role=Role.player,
    )
    db.add_all([winner, other])
    db.flush()
    db.add(Pick(
        user_id=winner.id, game_id=game.id, week_id=week.id, season_id=season.id,
        picked_team="KC", confidence_points=16, is_correct=True, points_earned=16,
    ))
    db.add(Pick(
        user_id=other.id, game_id=game.id, week_id=week.id, season_id=season.id,
        picked_team="BUF", confidence_points=16, is_correct=False, points_earned=0,
    ))
    plan = PayoutPlan(season_id=season.id, pool_amount=25, paid_weeks=18)
    db.add(plan)
    db.flush()
    db.add(PayoutRule(plan_id=plan.id, category="weekly", rank=1, amount=25))
    db.add(PayoutRule(plan_id=plan.id, category="season", rank=1, amount=100))
    db.commit()
    winner_id = winner.id
    db.close()
    return winner_id


def test_standings_shows_earned_between_points_and_correct():
    winner_id = build()
    client = TestClient(app, cookies={"access_token": create_access_token(winner_id)})
    html = client.get("/standings").text
    assert "Earned" in html
    # Weekly first place is locked in. The $100 season prize is still a
    # projection, so it must not show up as earned.
    assert "$25.00" in html
    assert "$100.00" not in html
    header = html[html.find("<thead>"):html.find("</thead>")]
    points = header.find("Total Pts")
    earned = header.find(">Earned<")
    correct = header.find("Correct")
    assert 0 < points < earned < correct, "Earned must sit between Total Pts and Correct"


if __name__ == "__main__":
    test_standings_shows_earned_between_points_and_correct()
    print("ok")
