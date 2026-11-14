"""The Simulator what-ifs remaining covers without touching the database.

Defaults assume the viewer wins every unfinished game they picked. Flipping
a cover changes simulated totals and the podium. The page puts the viewer's
column first, the same frozen-column convention as the all-picks matrix.

Run with: python tests/test_simulator.py
"""
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-sim-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, Season, User, Week
from app.routers import simulator
from app.services.simulator import (
    default_assumptions,
    podium_ranks,
    simulate_week,
)

app = FastAPI()
app.include_router(simulator.router)


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def build_league():
    """Locked week: one final, two remaining. Three players with opposing picks."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    now = datetime.utcnow()

    season = Season(year=2032, is_active=True)
    db.add(season)
    db.flush()

    week = Week(
        season_id=season.id,
        week_number=3,
        label="Week 3",
        first_kickoff=now - timedelta(hours=5),
        is_picks_locked=True,
    )
    db.add(week)
    db.flush()

    # Final: home covered — Alice (HOM) already banked 5.
    done = Game(
        week_id=week.id,
        home_team="HOM",
        away_team="AWY",
        kickoff_time=now - timedelta(hours=5),
        spread=-3.0,
        home_score=24,
        away_score=10,
        is_final=True,
        home_covered=True,
    )
    # Remaining: Alice on HOM2, Mira on AWY2 — Alice "wins" default is HOM2.
    live = Game(
        week_id=week.id,
        home_team="HOM2",
        away_team="AWY2",
        kickoff_time=now - timedelta(hours=1),
        spread=-2.5,
        home_score=14,
        away_score=7,
        is_in_progress=True,
    )
    upcoming = Game(
        week_id=week.id,
        home_team="HOM3",
        away_team="AWY3",
        kickoff_time=now + timedelta(hours=4),
        spread=-7.0,
    )
    db.add_all([done, live, upcoming])
    db.flush()

    alice = User(
        first_name="Alice", last_name="Ant", email="alice@x.com",
        password_hash="x", role=Role.player,
    )
    mira = User(
        first_name="Mira", last_name="Moth", email="mira@x.com",
        password_hash="x", role=Role.player,
    )
    zach = User(
        first_name="Zach", last_name="Zebra", email="zach@x.com",
        password_hash="x", role=Role.player,
    )
    db.add_all([alice, mira, zach])
    db.flush()

    # Alice: HOM 5, HOM2 10, HOM3 16 — defaults give her all remaining.
    # Mira:  AWY 5, AWY2 16, HOM3 10 — high on the live game Alice also has.
    # Zach:  HOM 3, AWY2 8, AWY3 12
    picks = [
        (alice, done, "HOM", 5, True, 5.0),
        (alice, live, "HOM2", 10, None, None),
        (alice, upcoming, "HOM3", 16, None, None),
        (mira, done, "AWY", 5, False, 0.0),
        (mira, live, "AWY2", 16, None, None),
        (mira, upcoming, "HOM3", 10, None, None),
        (zach, done, "HOM", 3, True, 3.0),
        (zach, live, "AWY2", 8, None, None),
        (zach, upcoming, "AWY3", 12, None, None),
    ]
    for person, game, team, pts, correct, earned in picks:
        db.add(Pick(
            user_id=person.id,
            game_id=game.id,
            week_id=week.id,
            season_id=season.id,
            picked_team=team,
            confidence_points=pts,
            is_correct=correct,
            points_earned=earned,
        ))
    db.commit()

    ids = {
        "week": week.id,
        "alice": alice.id,
        "mira": mira.id,
        "zach": zach.id,
        "done": done.id,
        "live": live.id,
        "upcoming": upcoming.id,
    }
    db.close()
    return ids


def test_default_assumptions_are_viewer_wins():
    ids = build_league()
    db = SessionLocal()
    games = db.query(Game).filter(Game.week_id == ids["week"]).all()
    viewer_picks = {
        p.game_id: p
        for p in db.query(Pick).filter(Pick.user_id == ids["alice"]).all()
    }
    assumed = default_assumptions(games, viewer_picks)
    assert ids["done"] not in assumed
    assert assumed[ids["live"]] is True, "Alice picked HOM2 — home should cover"
    assert assumed[ids["upcoming"]] is True, "Alice picked HOM3 — home should cover"

    mira_picks = {
        p.game_id: p
        for p in db.query(Pick).filter(Pick.user_id == ids["mira"]).all()
    }
    mira_assumed = default_assumptions(games, mira_picks)
    assert mira_assumed[ids["live"]] is False, "Mira picked AWY2 — away should cover"
    db.close()


def test_simulate_week_awards_defaults_and_respects_finals():
    ids = build_league()
    db = SessionLocal()
    games = db.query(Game).filter(Game.week_id == ids["week"]).all()
    users = db.query(User).order_by(User.id).all()
    pick_matrix = {}
    for p in db.query(Pick).filter(Pick.week_id == ids["week"]).all():
        pick_matrix.setdefault(p.user_id, {})[p.game_id] = p

    viewer_picks = pick_matrix[ids["alice"]]
    assumed = default_assumptions(games, viewer_picks)
    rows = simulate_week(games, users, pick_matrix, assumed)
    by_id = {r["user"].id: r for r in rows}

    # Alice: 5 (final) + 10 (live) + 16 (upcoming) = 31
    assert by_id[ids["alice"]]["total"] == 31
    # Mira: 0 (final miss) + 0 (live opposite) + 10 (same upcoming) = 10
    assert by_id[ids["mira"]]["total"] == 10
    # Zach: 3 + 0 + 0 = 3
    assert by_id[ids["zach"]]["total"] == 3

    # Flip the live game so AWY2 covers — Mira's 16 lands, Alice loses 10.
    assumed[ids["live"]] = False
    flipped = simulate_week(games, users, pick_matrix, assumed)
    by_id = {r["user"].id: r for r in flipped}
    assert by_id[ids["alice"]]["total"] == 21
    assert by_id[ids["mira"]]["total"] == 26
    assert by_id[ids["zach"]]["total"] == 11

    podium = podium_ranks(flipped)
    assert podium[ids["mira"]] == 1
    assert podium[ids["alice"]] == 2
    assert podium[ids["zach"]] == 3
    db.close()


def test_page_puts_viewer_first_and_defaults_to_their_wins():
    ids = build_league()
    html = client_for(ids["mira"]).get(f"/simulator?week_id={ids['week']}").text

    assert "Simulator" in html
    assert 'id="sim-matrix"' in html
    assert "Reset to my wins" in html

    # Viewer column first in the header.
    header = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
    names = re.findall(
        r'href="/profile/\d+"[^>]*>\s*([^<\s][^<]*?)\s*<', header
    )
    assert names[0] == "You", f"viewer not first: {names}"
    assert len(names) == 3

    # Payload defaults: Mira wins remaining → live away covers, upcoming home.
    payload = json.loads(
        re.search(
            r'<script id="sim-data" type="application/json">(.*?)</script>',
            html,
            re.S,
        ).group(1)
    )
    assert payload["assumptions"][str(ids["live"])] is False
    assert payload["assumptions"][str(ids["upcoming"])] is True
    assert payload["viewer_id"] == ids["mira"]

    # Podium on the default slate: Mira 0+16+10=26 first.
    totals_row = html.split('id="sim-totals"', 1)[1].split("</tr>", 1)[0]
    mira_cell = re.search(
        rf'data-user-id="{ids["mira"]}"[^>]*data-place="(\d+)"',
        totals_row,
    )
    assert mira_cell and mira_cell.group(1) == "1"


def test_simulator_hidden_before_lock():
    """An unlocked week must not appear — picks stay secret."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    now = datetime.utcnow()
    season = Season(year=2033, is_active=True)
    db.add(season)
    db.flush()
    week = Week(
        season_id=season.id,
        week_number=1,
        first_kickoff=now + timedelta(hours=10),
        is_picks_locked=False,
    )
    db.add(week)
    db.flush()
    game = Game(
        week_id=week.id,
        home_team="HOM",
        away_team="AWY",
        kickoff_time=now + timedelta(hours=10),
        spread=-3.0,
    )
    me = User(
        first_name="Pat", last_name="Player", email="pat@x.com",
        password_hash="x", role=Role.player,
    )
    db.add_all([game, me])
    db.flush()
    db.add(Pick(
        user_id=me.id, game_id=game.id, week_id=week.id,
        season_id=season.id, picked_team="HOM", confidence_points=1,
    ))
    db.commit()
    uid, wid = me.id, week.id
    db.close()

    html = client_for(uid).get("/simulator").text
    assert "No weeks with revealed picks yet" in html
    assert 'id="sim-matrix"' not in html
    # Direct week_id still refuses to show picks from an unrevealed week.
    html2 = client_for(uid).get(f"/simulator?week_id={wid}").text
    assert 'id="sim-matrix"' not in html2


def test_nav_link_present_via_template_compile():
    """Smoke: router responds for a logged-in user with a locked week."""
    ids = build_league()
    r = client_for(ids["alice"]).get("/simulator")
    assert r.status_code == 200
    assert b"Remaining games" in r.content or b"every game this week is final" in r.content


def test_script_terminator_in_a_name_stays_inside_json():
    """A stored ``</script>`` must not end #sim-data. The name stays data."""
    ids = build_league()
    payload = "</script><script>alert(1)</script>"
    db = SessionLocal()
    mira = db.query(User).filter(User.id == ids["mira"]).one()
    mira.first_name = payload
    mira.last_name = "O'Brien"
    db.commit()
    db.close()

    html = client_for(ids["alice"]).get(f"/simulator?week_id={ids['week']}").text
    assert "</script><script>" not in html
    assert "<script>alert" not in html

    body = re.search(
        r'<script id="sim-data" type="application/json">(.*?)</script>',
        html,
        re.S,
    ).group(1)
    assert "<" not in body
    data = json.loads(body)
    labels = [u["label"] for u in data["users"]]
    assert payload in labels
    # The visible name is escaped text, including the apostrophe.
    assert "&lt;/script&gt;" in html
    assert "O&#39;Brien" in html


if __name__ == "__main__":
    test_default_assumptions_are_viewer_wins()
    test_simulate_week_awards_defaults_and_respects_finals()
    test_page_puts_viewer_first_and_defaults_to_their_wins()
    test_simulator_hidden_before_lock()
    test_nav_link_present_via_template_compile()
    test_script_terminator_in_a_name_stays_inside_json()
    print("ok")
