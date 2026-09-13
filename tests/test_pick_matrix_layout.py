"""The all-picks matrix keeps the viewer's own column in the frozen block.

The table is wider than a phone, so the first four columns (game, spread,
result and the viewer's own picks) are frozen while the rest scroll sideways.
Three of those four are fixed by the template, but the fourth depends on the
route ordering the players so the viewer comes first — get that wrong and
everyone compares against whichever player happens to sort first, which is the
one thing the frozen column is there to prevent.

Run with: python tests/test_pick_matrix_layout.py
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-matrix-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, Season, User, Week
from app.routers import picks

app = FastAPI()
app.include_router(picks.router)


def build_league():
    """One locked week, three players whose names sort away from each other."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    season = Season(year=2031, is_active=True)
    db.add(season)
    db.flush()

    week = Week(
        season_id=season.id, week_number=1, label="Week 1",
        first_kickoff=datetime.utcnow() - timedelta(hours=2),
        is_picks_locked=True,
    )
    db.add(week)
    db.flush()

    game = Game(
        week_id=week.id, home_team="HOM", away_team="AWY",
        kickoff_time=datetime.utcnow() - timedelta(hours=2), spread=-3.0,
    )
    db.add(game)
    db.flush()

    people = {
        "alice": User(first_name="Alice", last_name="Ant", email="a@x.com",
                      password_hash="x", role=Role.player),
        "mira": User(first_name="Mira", last_name="Moth", email="m@x.com",
                     password_hash="x", role=Role.player),
        "zach": User(first_name="Zach", last_name="Zebra", email="z@x.com",
                     password_hash="x", role=Role.player),
    }
    db.add_all(people.values())
    db.flush()

    for person in people.values():
        db.add(Pick(
            user_id=person.id, game_id=game.id, week_id=week.id,
            season_id=season.id, picked_team="HOM", confidence_points=1,
        ))
    db.commit()

    ids = {name: person.id for name, person in people.items()}
    ids["week"] = week.id
    db.close()
    return ids


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def header_names(html):
    """The player names in the matrix header, left to right.

    Scoped past the week-standings table above it, which has a thead of its own.
    """
    matrix = html.split("<!-- Pick matrix -->", 1)[1]
    header = matrix.split("<thead>", 1)[1].split("</thead>", 1)[0]
    return re.findall(r'href="/profile/\d+"[^>]*>\s*([^<\s][^<]*?)\s*<', header)


def test_the_viewers_column_is_the_frozen_one():
    ids = build_league()
    for viewer in ("alice", "mira", "zach"):
        html = client_for(ids[viewer]).get(f"/picks/week/{ids['week']}/all").text
        names = header_names(html)
        assert names[0] == "You", f"{viewer}'s own column is not first — header reads {names}"
        assert names.count("You") == 1, f"more than one column is labelled You: {names}"
        # Every other player is still there, just after the frozen block.
        assert len(names) == 3, f"players went missing from the header: {names}"


def test_the_frozen_columns_and_header_are_marked_up():
    ids = build_league()
    html = client_for(ids["mira"]).get(f"/picks/week/{ids['week']}/all").text
    assert "pm-wrap" in html, "the matrix is not inside the scroll container that the freeze needs"
    for column in ("pm-c1", "pm-c2", "pm-c3", "pm-c4"):
        assert column in html, f"frozen column {column} is missing from the matrix"
    assert "pm-you" in html, "the viewer's column is not tinted as theirs"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
