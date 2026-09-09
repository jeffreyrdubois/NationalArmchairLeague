"""Regression tests for pick secrecy before a week locks.

The pool only works if nobody can read another player's picks while there is
still time to change their own. These tests drive the real routes and assert
on the rendered HTML, because the leak that matters is the one a browser can
see — it is not enough for the route to be careful if a template renders the
picks anyway.

Admins are checked alongside players on purpose: the commissioner plays in the
pool too, so an admin peek before the lock is the same unfair advantage.

Run with: python tests/test_pick_visibility.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# A file-backed scratch database: the TestClient runs the app on another
# thread, which an in-memory SQLite would not share. Never a real database,
# whatever DATABASE_URL says.
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-visibility-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, Season, User, Week
from app.routers import dashboard, picks

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(picks.router)
app.include_router(dashboard.router)

# Distinctive abbreviations so "did a pick leak?" is a plain substring check
# that cannot be satisfied by the schedule, the spreads or the scores.
PICKED_TEAMS = ["QQA", "QQB", "QQC"]

_next_year = iter(range(2026, 2100))


def build_league(locked: bool):
    """A season with one week, three players, and picks in for two of them.

    ``carol`` deliberately has no picks, so the submission board has someone
    to report as outstanding.
    """
    # Each case gets a clean league: the routes resolve "the season" by
    # is_active, so leftovers from an earlier case would be the one they find.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    year = next(_next_year)
    season = Season(year=year, is_active=True)
    db.add(season)
    db.flush()

    week = Week(
        season_id=season.id,
        week_number=1,
        label=f"Week {year}",
        first_kickoff=datetime.utcnow() + timedelta(days=2),
        is_picks_locked=locked,
    )
    db.add(week)
    db.flush()

    games = [
        Game(
            week_id=week.id,
            home_team=abbr,
            away_team=f"O{i}",
            kickoff_time=datetime.utcnow() + timedelta(days=2, hours=i),
            spread=-3.0,
        )
        for i, abbr in enumerate(PICKED_TEAMS)
    ]
    db.add_all(games)
    db.flush()

    people = {
        "alice": User(first_name="Alice", last_name="Ant", email=f"a{year}@x.com",
                      password_hash="x", role=Role.player),
        "bob": User(first_name="Bob", last_name="Bee", email=f"b{year}@x.com",
                    password_hash="x", role=Role.player),
        "carol": User(first_name="Carol", last_name="Cod", email=f"c{year}@x.com",
                      password_hash="x", role=Role.player),
        "admin": User(first_name="Admin", last_name="Ace", email=f"n{year}@x.com",
                      password_hash="x", role=Role.admin),
    }
    db.add_all(people.values())
    db.flush()

    for person in (people["alice"], people["bob"]):
        for i, game in enumerate(games):
            db.add(Pick(
                user_id=person.id,
                game_id=game.id,
                week_id=week.id,
                season_id=season.id,
                picked_team=game.home_team,
                confidence_points=14 + i,
            ))
    db.commit()

    ids = {name: person.id for name, person in people.items()}
    ids["week"] = week.id
    ids["season_year"] = year
    db.close()
    return ids


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def leaked_picks(html):
    """Which players' picked teams are readable in this page."""
    return [abbr for abbr in PICKED_TEAMS if abbr in html]


def assert_no_leak(html, where):
    leaked = leaked_picks(html)
    assert not leaked, f"{where} leaked picked teams {leaked}"


# --------------------------------------------------------------------------
# Before the lock: no picks anywhere, for anybody
# --------------------------------------------------------------------------

def test_all_picks_page_hides_picks_before_lock():
    ids = build_league(locked=False)
    for viewer in ("alice", "carol", "admin"):
        r = client_for(ids[viewer]).get(f"/picks/week/{ids['week']}/all")
        assert r.status_code == 200, f"{viewer} got {r.status_code}"
        assert_no_leak(r.text, f"/picks/week/all as {viewer}")


def test_profile_hides_another_players_picks_before_lock():
    ids = build_league(locked=False)
    for viewer in ("alice", "carol", "admin"):
        r = client_for(ids[viewer]).get(f"/profile/{ids['bob']}")
        assert r.status_code == 200
        assert_no_leak(r.text, f"/profile/bob as {viewer}")


def test_standings_hides_picks_before_lock():
    ids = build_league(locked=False)
    for viewer in ("alice", "admin"):
        r = client_for(ids[viewer]).get("/standings")
        assert r.status_code == 200
        assert_no_leak(r.text, f"/standings as {viewer}")


def test_owner_still_sees_their_own_picks_before_lock():
    ids = build_league(locked=False)
    r = client_for(ids["bob"]).get(f"/profile/{ids['bob']}")
    assert r.status_code == 200
    assert leaked_picks(r.text) == PICKED_TEAMS, "a player lost sight of their own picks"


# --------------------------------------------------------------------------
# Before the lock: who has submitted is public
# --------------------------------------------------------------------------

def test_submission_status_is_visible_before_lock():
    ids = build_league(locked=False)
    html = client_for(ids["carol"]).get(f"/picks/week/{ids['week']}/all").text
    assert "Who&rsquo;s Picked" in html or "Who’s Picked" in html
    assert "Alice" in html and "Bob" in html and "Carol" in html
    assert "Not submitted" in html, "the player with no picks was not flagged"
    assert "2 of 4 in" in html, "submitted count is wrong"
    assert_no_leak(html, "the submission board")


def test_standings_shows_who_is_in_before_lock():
    ids = build_league(locked=False)
    html = client_for(ids["carol"]).get("/standings").text
    assert "Who&rsquo;s picked" in html or "Who’s picked" in html
    assert "2 of 4 players are in" in html
    assert_no_leak(html, "the standings submission board")


def test_profile_reports_submission_without_picks():
    ids = build_league(locked=False)
    html = client_for(ids["alice"]).get(f"/profile/{ids['bob']}").text
    assert "3 picks submitted" in html
    assert "Hidden until the first kickoff" in html
    assert_no_leak(html, "bob's profile")


# --------------------------------------------------------------------------
# After the lock: everything opens up
# --------------------------------------------------------------------------

def test_picks_are_revealed_once_the_week_locks():
    ids = build_league(locked=True)
    for viewer in ("alice", "carol", "admin"):
        html = client_for(ids[viewer]).get(f"/picks/week/{ids['week']}/all").text
        assert leaked_picks(html) == PICKED_TEAMS, f"{viewer} cannot see picks after the lock"

    html = client_for(ids["alice"]).get(f"/profile/{ids['bob']}").text
    assert leaked_picks(html) == PICKED_TEAMS, "profile stayed hidden after the lock"


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
