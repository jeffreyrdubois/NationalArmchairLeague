"""A live score should say whether your pick is covering, before the game is final.

is_correct stays empty until the whistle, which left a game that was already
20 points the wrong way looking the same as one that had not kicked off.
The dashboard and the current week's picks page outline your own pick from
the score as it stands. Nobody else's cell changes, and a finished game
keeps the settled green or red.

Run with: python tests/test_live_covering.py
"""
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-live-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, Season, User, Week
from app.routers import dashboard, picks
from app.services.scoring import live_covering

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(picks.router)
app.include_router(dashboard.router)


def test_live_covering_reads_the_score_and_ignores_a_final():
    now = datetime.utcnow()
    game = Game(
        home_team="HOM", away_team="AWY", spread=-3.5,
        home_score=10, away_score=0,
        kickoff_time=now - timedelta(hours=1), is_final=False, is_in_progress=True,
    )
    home = Pick(picked_team="HOM")
    away = Pick(picked_team="AWY")
    assert live_covering(home, game) is True
    assert live_covering(away, game) is False

    game.is_final = True
    assert live_covering(home, game) is None, "a final belongs to is_correct"

    game.is_final = False
    game.home_score = None
    assert live_covering(home, game) is None, "no score yet is not a loss"


def test_pages_outline_only_your_live_pick():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    now = datetime.utcnow()
    season = Season(year=2099, is_active=True)
    db.add(season)
    db.flush()
    week = Week(
        season_id=season.id, week_number=1, first_kickoff=now - timedelta(hours=2),
        is_picks_locked=True,
    )
    db.add(week)
    db.flush()
    ahead = Game(
        week_id=week.id, home_team="HOM", away_team="AWY", spread=-3.0,
        home_score=17, away_score=7, kickoff_time=now - timedelta(hours=1),
        is_in_progress=True,
    )
    behind = Game(
        week_id=week.id, home_team="HOM2", away_team="AWY2", spread=-3.0,
        home_score=0, away_score=21, kickoff_time=now - timedelta(minutes=30),
        is_in_progress=True,
    )
    waiting = Game(
        week_id=week.id, home_team="HOM3", away_team="AWY3", spread=-3.0,
        kickoff_time=now + timedelta(hours=3),
    )
    db.add_all([ahead, behind, waiting])
    db.flush()
    me = User(first_name="Ada", last_name="One", email="ada@x.com",
              password_hash="x", role=Role.player)
    other = User(first_name="Bea", last_name="Two", email="bea@x.com",
                 password_hash="x", role=Role.player)
    db.add_all([me, other])
    db.flush()
    for person, teams in (
        (me, [ahead.home_team, behind.home_team, waiting.home_team]),
        (other, [ahead.away_team, behind.away_team, waiting.away_team]),
    ):
        for i, (game, team) in enumerate(zip((ahead, behind, waiting), teams)):
            db.add(Pick(
                user_id=person.id, game_id=game.id, week_id=week.id,
                season_id=season.id, picked_team=team, confidence_points=i + 1,
            ))
    db.commit()
    week_id, me_id = week.id, me.id
    db.close()

    client = TestClient(app, cookies={"access_token": create_access_token(me_id)})
    home = client.get("/").text
    assert "pick-live-ahead" in home and "Covering" in home
    assert "pick-live-behind" in home and "Not covering" in home
    assert "pick-pending" in home, "a game with no score stays pending"
    assert "#fef2f2" not in home and "#f0fdf4" not in home, "a live card must not be filled in"

    grid = client.get(f"/picks/week/{week_id}/all").text
    assert grid.count('title="Covering on the current score"') == 1
    assert grid.count('title="Not covering on the current score"') == 1
    # Earned is only finished games. Pending is what the week would be if every
    # game already in progress ended at the current score.
    assert re.findall(r'pm-earned">(\d+)', grid) == ["0", "0"]
    assert re.findall(r'pm-pending[^"]*">(\d+)', grid) == ["1", "2"]


if __name__ == "__main__":
    test_live_covering_reads_the_score_and_ignores_a_final()
    test_pages_outline_only_your_live_pick()
    print("ok")
