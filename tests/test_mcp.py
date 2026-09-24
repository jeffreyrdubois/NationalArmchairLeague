"""Tests for the MCP server at /mcp.

These speak real MCP over HTTP to the real endpoint, the way Claude does, so
they cover the three things that matter:

1. **Nobody gets in without a token**, and a token only works while its owner
   is someone who may hold one.
2. **Pick secrecy survives a new door.** Before a week locks, the results tool
   shows who has submitted, never what anyone picked — the same rule the site
   enforces in app/services/visibility.py.
3. **Picks entered through Claude obey the pick sheet's rules**: one value per
   game, each value once, nothing saved from a submission that is invalid
   anywhere, nothing at all once the week locks.

Run with: python tests/test_mcp.py
"""
import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# A scratch file, never a real database: the endpoint opens its own sessions.
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-mcp-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Route

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.mcp_server import create_endpoint, mcp
from app.models import (
    AppSetting, Game, McpToken, PayoutPlan, PayoutRule, Pick, Role, Season,
    Transaction, User, Week,
)
from app.routers import dashboard
from app.services import mcp_tokens
from app.services.scoring import update_game_results


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(dashboard.router)
app.router.routes.append(
    Route("/mcp", endpoint=create_endpoint(), methods=["GET", "POST", "DELETE"])
)

Base.metadata.create_all(bind=engine)

_ids = iter(range(1, 10_000))


# ---------------------------------------------------------------------------
# Fixture: one active season, an open week 2 and a finished week 1
# ---------------------------------------------------------------------------

def build_league():
    db = SessionLocal()
    admin = User(first_name="Ada", last_name="Admin", email="ada@example.com",
                 password_hash="x", role=Role.admin)
    player = User(first_name="Pat", last_name="Player", email="pat@example.com",
                  password_hash="x", role=Role.player)
    db.add_all([admin, player])
    season = Season(year=2026, is_active=True)
    db.add(season)
    db.flush()

    past = datetime.utcnow() - timedelta(days=3)
    future = datetime.utcnow() + timedelta(days=3)
    week1 = Week(season_id=season.id, week_number=1, label="Week 1",
                 first_kickoff=past, is_picks_locked=True, is_completed=True)
    week2 = Week(season_id=season.id, week_number=2, label="Week 2",
                 first_kickoff=future)
    db.add_all([week1, week2])
    db.flush()

    def game(week, away, home, spread, when, away_name, home_name):
        g = Game(week_id=week.id, espn_game_id=f"g{next(_ids)}",
                 away_team=away, home_team=home, spread=spread,
                 kickoff_time=when, away_team_name=away_name,
                 home_team_name=home_name)
        db.add(g)
        return g

    w1 = [
        game(week1, "BUF", "KC", -3.0, past, "Buffalo Bills", "Kansas City Chiefs"),
        game(week1, "DAL", "PHI", -2.5, past, "Dallas Cowboys", "Philadelphia Eagles"),
    ]
    w2 = [
        game(week2, "NYJ", "NE", 1.5, future, "New York Jets", "New England Patriots"),
        game(week2, "SF", "SEA", 3.0, future, "San Francisco 49ers", "Seattle Seahawks"),
        game(week2, "GB", "CHI", 0.0, future, "Green Bay Packers", "Chicago Bears"),
    ]
    db.flush()

    # Week 1: home covers both. Ada picks home (16, 15) — 31 points.
    # Pat picks home for 16 and away for 15 — 16 points.
    db.add_all([
        Pick(user_id=admin.id, game_id=w1[0].id, week_id=week1.id, season_id=season.id,
             picked_team="KC", confidence_points=16),
        Pick(user_id=admin.id, game_id=w1[1].id, week_id=week1.id, season_id=season.id,
             picked_team="PHI", confidence_points=15),
        Pick(user_id=player.id, game_id=w1[0].id, week_id=week1.id, season_id=season.id,
             picked_team="KC", confidence_points=16),
        Pick(user_id=player.id, game_id=w1[1].id, week_id=week1.id, season_id=season.id,
             picked_team="DAL", confidence_points=15),
    ])
    for g, (away, home) in zip(w1, [(20, 27), (17, 24)]):
        g.away_score, g.home_score, g.is_final = away, home, True
    db.commit()
    for g in w1:
        update_game_results(db, g)

    # Week 2: Pat has picks in already — which must stay secret from Ada.
    db.add(Pick(user_id=player.id, game_id=w2[2].id, week_id=week2.id,
                season_id=season.id, picked_team="GB", confidence_points=16))

    # Money: $10 a week to first, $50 entry fee Pat has half paid.
    plan = PayoutPlan(season_id=season.id, pool_amount=100, paid_weeks=18)
    db.add(plan)
    db.flush()
    db.add(PayoutRule(plan_id=plan.id, category="weekly", rank=1, amount=10))
    db.add(AppSetting(key="entry_fee", value="50"))
    db.add(Transaction(user_id=player.id, amount=25, direction="in",
                       note="half", logged_by_id=admin.id))
    db.add(Transaction(user_id=admin.id, amount=50, direction="in",
                       note="paid", logged_by_id=admin.id))
    db.commit()
    ids = {
        "admin": admin.id, "player": player.id,
        "week1": week1.id, "week2": week2.id,
        "w2_games": [g.id for g in w2],
    }
    db.close()
    return ids


# ---------------------------------------------------------------------------
# Speaking MCP
# ---------------------------------------------------------------------------

HEADERS = {"Accept": "application/json, text/event-stream"}


def rpc(client, token, method, params=None, *, raw=False):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params or {},
    })
    if raw:
        return resp
    assert resp.status_code == 200, (resp.status_code, resp.text)
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def call(client, token, tool, **args):
    """Call a tool; returns (is_error, payload-or-message)."""
    result = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    if result.get("isError"):
        return True, result["content"][0]["text"]
    return False, result.get("structuredContent") or json.loads(result["content"][0]["text"])


def ok(client, token, tool, **args):
    is_error, payload = call(client, token, tool, **args)
    assert not is_error, payload
    return payload


def err(client, token, tool, **args):
    is_error, payload = call(client, token, tool, **args)
    assert is_error, f"{tool} should have failed: {payload}"
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_requests_without_a_valid_token_are_refused(client, ids):
    assert rpc(client, None, "tools/list", raw=True).status_code == 401
    assert rpc(client, "nal_not-a-real-token", "tools/list", raw=True).status_code == 401


def test_settings_issues_a_token_once_and_stores_only_its_hash(client, ids):
    client.cookies.set("access_token", create_access_token(ids["admin"]))
    page = client.post("/settings/mcp-token")
    client.cookies.clear()
    assert page.status_code == 200
    token = page.text.split('value="nal_', 1)[1].split('"', 1)[0]
    token = "nal_" + token

    db = SessionLocal()
    row = db.query(McpToken).filter(McpToken.user_id == ids["admin"]).one()
    assert row.token_hash == mcp_tokens.hash_token(token)
    assert token not in (row.token_hash, row.token_prefix)
    db.close()

    tools = {t["name"] for t in rpc(client, token, "tools/list")["tools"]}
    assert tools == {
        "get_week_results", "get_season_standings", "get_award_standings",
        "get_money_owed", "get_pick_sheet", "submit_picks",
    }, tools

    # The token also works as a query parameter, for URL-only clients.
    resp = client.post(f"/mcp?token={token}", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert resp.status_code == 200
    return token


def test_players_cannot_hold_a_token_yet(client, ids):
    client.cookies.set("access_token", create_access_token(ids["player"]))
    page = client.post("/settings/mcp-token", follow_redirects=False)
    client.cookies.clear()
    assert page.status_code == 303

    # Nor does one made behind the page's back work.
    db = SessionLocal()
    token = mcp_tokens.issue_token(db, db.get(User, ids["player"]))
    db.close()
    assert rpc(client, token, "tools/list", raw=True).status_code == 401


def test_open_week_results_show_submissions_not_picks(client, token):
    result = ok(client, token, "get_week_results", week_number=2)
    assert result["picks_locked"] is False
    assert "leaderboard" not in result
    by_player = {s["player"]: s for s in result["submissions"]}
    assert by_player["Pat Player"]["picks_made"] == 1
    # Ada has no picks this week, so no game may carry a pick at all.
    assert all("your_pick" not in g for g in result["games"]), result["games"]


def test_week_results_default_to_latest_locked_week(client, token):
    result = ok(client, token, "get_week_results")
    assert result["week_number"] == 1
    board = {row["player"]: row for row in result["leaderboard"]}
    assert board["Ada Admin"]["points"] == 31 and board["Ada Admin"]["place"] == 1
    assert board["Pat Player"]["points"] == 16 and board["Pat Player"]["place"] == 2
    games = {g["matchup"]: g for g in result["games"]}
    assert games["BUF @ KC"]["covered"] == "KC"
    assert games["BUF @ KC"]["spread"] == "KC -3"
    assert games["DAL @ PHI"]["your_pick"]["result"] == "correct"


def test_season_standings(client, token):
    result = ok(client, token, "get_season_standings")
    top, second = result["standings"]
    assert (top["player"], top["weeks_won"], top["is_you"]) == ("Ada Admin", 1, True)
    assert second["behind_leader"] == 15


def test_award_standings(client, token):
    result = ok(client, token, "get_award_standings", award="contrarian")
    assert [a["id"] for a in result["awards"]] == ["the_contrarian"]
    assert result["awards"][0]["leaders"][0]["player"] == "Ada Admin"
    assert "No award matches" in err(client, token, "get_award_standings", award="nope")


def test_money_owed(client, token):
    result = ok(client, token, "get_money_owed")
    owed = {row["player"]: row for row in result["prizes_owed"]}
    assert owed["Ada Admin"]["owed_now"] == 10.0
    assert owed["Ada Admin"]["prizes"][0]["for"] == "Week 1"
    assert "Pat Player" not in owed
    fees = {row["player"]: row for row in result["entry_fees_outstanding"]}
    assert fees == {"Pat Player": {"player": "Pat Player", "paid": 25.0, "still_owes": 25.0}}


def test_submit_picks_fills_in_and_rearranges(client, token):
    sheet = ok(client, token, "get_pick_sheet")
    assert sheet["week_number"] == 2
    assert sheet["point_values"] == [14, 15, 16]
    assert sheet["complete"] is False

    # A partial submission, by abbreviation and by name.
    sheet = ok(client, token, "submit_picks", picks=[
        {"team": "NE", "points": 16}, {"team": "49ers", "points": 15},
    ])
    assert sheet["games_without_a_pick"] == ["GB @ CHI"]
    assert sheet["unused_point_values"] == [14]

    # Filling the last game, and swapping two values that are both in use.
    sheet = ok(client, token, "submit_picks", picks=[
        {"team": "Bears", "points": 14},
        {"team": "NE", "points": 15}, {"team": "SF", "points": 16},
    ])
    assert sheet["complete"] is True
    mine = {g["matchup"]: g["your_pick"] for g in sheet["games"]}
    assert mine == {
        "NYJ @ NE": {"team": "NE", "points": 15},
        "SF @ SEA": {"team": "SF", "points": 16},
        "GB @ CHI": {"team": "CHI", "points": 14},
    }


def test_invalid_submissions_change_nothing(client, token, ids):
    before = ok(client, token, "get_pick_sheet")["games"]

    msg = err(client, token, "submit_picks", picks=[{"team": "CHI", "points": 16}])
    assert "16 points would be on both" in msg, msg
    msg = err(client, token, "submit_picks", picks=[{"team": "NE", "points": 3}])
    assert "not a point value" in msg, msg
    msg = err(client, token, "submit_picks", picks=[{"team": "Rams", "points": 14}])
    assert "No team matching" in msg, msg
    msg = err(client, token, "submit_picks", picks=[
        {"team": "NE", "points": 14}, {"team": "NYJ", "points": 15},
    ])
    assert "picked more than once" in msg, msg

    assert ok(client, token, "get_pick_sheet")["games"] == before

    # Pat's secret week-2 pick was never touched by any of Ada's saves.
    db = SessionLocal()
    pat = db.query(Pick).filter(Pick.user_id == ids["player"], Pick.week_id == ids["week2"]).all()
    assert [(p.picked_team, p.confidence_points) for p in pat] == [("GB", 16)]
    db.close()


def test_locked_week_refuses_picks(client, token, ids):
    db = SessionLocal()
    db.get(Week, ids["week2"]).is_picks_locked = True
    db.commit()
    db.close()
    msg = err(client, token, "submit_picks", picks=[{"team": "NE", "points": 14}])
    assert "locked" in msg, msg
    db = SessionLocal()
    db.get(Week, ids["week2"]).is_picks_locked = False
    db.commit()
    db.close()


def test_a_deactivated_owner_loses_access(client, token, ids):
    db = SessionLocal()
    db.get(User, ids["admin"]).is_active = False
    db.commit()
    assert rpc(client, token, "tools/list", raw=True).status_code == 401
    db.get(User, ids["admin"]).is_active = True
    db.commit()
    db.close()


if __name__ == "__main__":
    ids = build_league()
    with TestClient(app) as client:
        test_requests_without_a_valid_token_are_refused(client, ids)
        token = test_settings_issues_a_token_once_and_stores_only_its_hash(client, ids)
        test_players_cannot_hold_a_token_yet(client, ids)
        test_open_week_results_show_submissions_not_picks(client, token)
        test_week_results_default_to_latest_locked_week(client, token)
        test_season_standings(client, token)
        test_award_standings(client, token)
        test_money_owed(client, token)
        test_submit_picks_fills_in_and_rearranges(client, token)
        test_invalid_submissions_change_nothing(client, token, ids)
        test_locked_week_refuses_picks(client, token, ids)
        test_a_deactivated_owner_loses_access(client, token, ids)
    print("all MCP tests passed")
