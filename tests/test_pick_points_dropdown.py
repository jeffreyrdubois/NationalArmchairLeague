"""A confidence value already used on one game stays out of the others.

The pick sheet hands out each point value once. Opening the points menu on
a game used to list the whole range, including values already sitting on
another game, and the duplicate only showed up after you picked it. The
menu should offer what is still free, while the game that already has a
value keeps that value so it can be seen and changed.

Run with: python tests/test_pick_points_dropdown.py
"""
import os
import sys
import tempfile
from html.parser import HTMLParser

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# A file-backed scratch database: the TestClient runs the app on another
# thread, which an in-memory SQLite would not share.
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-points-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, Season, User, Week
from app.routers import picks
from app.services import espn

Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(picks.router)


async def _no_records():
    return {}


# The picks page asks ESPN for team records. A miss is harmless, but it
# waits on the network, and this test is about the menus.
espn.fetch_team_records = _no_records

_next_year = iter(range(2026, 2100))


class _MenuParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.menus = []
        self._menu = None
        self._option = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select" and str(attrs.get("name", "")).endswith("_points"):
            self._menu = {"name": attrs["name"], "options": []}
            self.menus.append(self._menu)
        elif tag == "option" and self._menu is not None:
            self._option = {
                "value": attrs.get("value", ""),
                "selected": "selected" in attrs,
            }
            self._text = []

    def handle_endtag(self, tag):
        if tag == "option" and self._option is not None:
            self._option["text"] = "".join(self._text).strip()
            self._menu["options"].append(self._option)
            self._option = None
        elif tag == "select":
            self._menu = None

    def handle_data(self, data):
        if self._option is not None:
            self._text.append(data)


def point_menus(html):
    parser = _MenuParser()
    parser.feed(html)
    return {menu["name"]: menu for menu in parser.menus}


def option_values(menu):
    return [opt["value"] for opt in menu["options"]]


def selected_value(menu):
    chosen = [opt["value"] for opt in menu["options"] if opt["selected"]]
    assert len(chosen) == 1, chosen
    return chosen[0]


def build_week(n_games, assigned):
    """One player, one open week, and confidence points on some of the games.

    ``assigned`` maps a game index to the points already saved on it.
    """
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
        is_spreads_locked=True,
    )
    db.add(week)
    db.flush()
    games = []
    for i in range(n_games):
        game = Game(
            week_id=week.id,
            home_team=f"H{i}",
            away_team=f"A{i}",
            home_team_name=f"Home {i}",
            away_team_name=f"Away {i}",
            kickoff_time=datetime.utcnow() + timedelta(days=2, hours=i),
            spread=-3.0,
        )
        db.add(game)
        games.append(game)
    db.flush()
    user = User(
        first_name="Pat",
        last_name="Picker",
        email=f"pat{year}@x.com",
        password_hash="x",
        role=Role.player,
    )
    db.add(user)
    db.flush()
    for index, points in assigned.items():
        game = games[index]
        db.add(Pick(
            user_id=user.id,
            game_id=game.id,
            week_id=week.id,
            season_id=season.id,
            picked_team=game.home_team,
            confidence_points=points,
        ))
    db.commit()
    ids = {"user": user.id, "games": [game.id for game in games]}
    db.close()
    return ids


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def menus_for(ids):
    response = client_for(ids["user"]).get("/picks")
    assert response.status_code == 200, response.status_code
    assert "/static/js/pick-points.js" in response.text
    assert "syncPointMenus(" in response.text
    menus = point_menus(response.text)
    assert list(menus) == [f"game_{gid}_points" for gid in ids["games"]], list(menus)
    return menus


def test_an_unused_slate_offers_every_point_on_every_game():
    ids = build_week(3, {})
    menus = menus_for(ids)
    for menu in menus.values():
        assert option_values(menu) == ["", "14", "15", "16"], option_values(menu)
        assert selected_value(menu) == ""
        assert menu["options"][0]["text"] == "-- Select points --"


def test_a_value_already_chosen_is_left_out_of_the_other_menus():
    """16 is on the first game and 14 on the second, so only 15 is free."""
    ids = build_week(3, {0: 16, 1: 14})
    menus = menus_for(ids)
    first, second, third = [menus[f"game_{gid}_points"] for gid in ids["games"]]

    assert option_values(first) == ["", "15", "16"], option_values(first)
    assert selected_value(first) == "16"
    assert first["options"][-1]["text"] == "16 points"

    assert option_values(second) == ["", "14", "15"], option_values(second)
    assert selected_value(second) == "14"

    assert option_values(third) == ["", "15"], option_values(third)
    assert selected_value(third) == ""
    assert "14" not in option_values(third)
    assert "16" not in option_values(third)


def test_the_one_point_label_stays_singular():
    ids = build_week(16, {0: 1})
    menus = menus_for(ids)
    first = menus[f"game_{ids['games'][0]}_points"]
    other = menus[f"game_{ids['games'][1]}_points"]
    chosen = next(opt for opt in first["options"] if opt["selected"])
    assert chosen["value"] == "1"
    assert chosen["text"] == "1 point"
    assert "1" not in option_values(other)


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
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
