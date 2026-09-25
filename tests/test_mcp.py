"""Tests for the MCP server at /mcp.

These speak real MCP over HTTP to the real endpoint, the way Claude does, so
they cover the three things that matter:

1. **Nobody gets in without going through OAuth** — a registered client, its
   secret, PKCE, and a person who may connect approving it — and a token only
   works while that person still may.
2. **Pick secrecy survives a new door.** Before a week locks, the results tool
   shows who has submitted, never what anyone picked — the same rule the site
   enforces in app/services/visibility.py.
3. **Picks entered through Claude obey the pick sheet's rules**: one value per
   game, each value once, nothing saved from a submission that is invalid
   anywhere, nothing at all once the week locks.

Run with: python tests/test_mcp.py
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

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
    AppSetting, Game, OAuthClient, PayoutPlan, PayoutRule, Pick, Role, Season,
    Transaction, User, Week,
)
from app.routers import dashboard, oauth as oauth_router
from app.routers.auth import safe_next
from app.services import oauth
from app.services.scoring import update_game_results


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(oauth_router.router)
app.router.routes.append(
    Route("/mcp", endpoint=create_endpoint(), methods=["GET", "POST", "DELETE"])
)

Base.metadata.create_all(bind=engine)

_ids = iter(range(1, 10_000))

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


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
    resp = rpc(client, None, "tools/list", raw=True)
    assert resp.status_code == 401
    # The 401 is what starts Claude's OAuth discovery, so it has to say where.
    challenge = resp.headers["www-authenticate"]
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in challenge
    assert rpc(client, "not-a-real-token", "tools/list", raw=True).status_code == 401


def test_discovery_metadata(client, ids):
    resource = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == "http://testserver/mcp"
    assert resource["authorization_servers"] == ["http://testserver"]
    # Behind the reverse proxy the scheme the outside world used comes through.
    resource = client.get("/.well-known/oauth-protected-resource",
                          headers={"X-Forwarded-Proto": "https"}).json()
    assert resource["resource"] == "https://testserver/mcp"

    server = client.get("/.well-known/oauth-authorization-server").json()
    assert server["authorization_endpoint"] == "http://testserver/oauth/authorize"
    assert server["token_endpoint"] == "http://testserver/oauth/token"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert "registration_endpoint" not in server


def create_client_as_admin(client, ids):
    """Create an OAuth client from the settings page; returns (id, secret)."""
    client.cookies.set("access_token", create_access_token(ids["admin"]))
    page = client.post("/settings/mcp-clients", data={
        "name": "Claude",
        "redirect_uris": "\n".join(oauth.DEFAULT_REDIRECT_URIS),
    })
    client.cookies.clear()
    assert page.status_code == 200, page.text
    client_id = "nal_" + page.text.split('value="nal_', 1)[1].split('"', 1)[0]
    secret = "nalsec_" + page.text.split('value="nalsec_', 1)[1].split('"', 1)[0]
    return client_id, secret


def pkce():
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize(client, user_id, client_id, challenge,
              redirect_uri=CALLBACK, decision="allow"):
    """Log in as ``user_id``, open the approval page and answer it."""
    params = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "state": "xyz",
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    client.cookies.set("access_token", create_access_token(user_id))
    page = client.get("/oauth/authorize", params=params)
    if page.status_code != 200 or "Allow" not in page.text:
        client.cookies.clear()
        return page
    resp = client.post("/oauth/authorize", data={**params, "decision": decision},
                       follow_redirects=False)
    client.cookies.clear()
    return resp


def code_from(resp):
    assert resp.status_code == 303, resp.text
    query = dict(parse_qsl(urlsplit(resp.headers["location"]).query))
    assert query["state"] == "xyz"
    return query


def connect(client, ids, client_id, secret):
    """The whole flow Claude runs; returns the token response."""
    verifier, challenge = pkce()
    code = code_from(authorize(client, ids["admin"], client_id, challenge))["code"]
    resp = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": CALLBACK, "code_verifier": verifier,
        "client_id": client_id, "client_secret": secret,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_full_oauth_flow_connects_claude(client, ids):
    client_id, secret = create_client_as_admin(client, ids)

    # Only a hash of the secret is kept.
    db = SessionLocal()
    row = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).one()
    assert secret not in row.secret_hash
    db.close()

    tokens = connect(client, ids, client_id, secret)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]
    tools = {t["name"] for t in rpc(client, tokens["access_token"], "tools/list")["tools"]}
    assert tools == {
        "get_week_results", "get_season_standings", "get_award_standings",
        "get_money_owed", "get_pick_sheet", "submit_picks",
        "get_week_picks", "get_rooting_guide",
    }, tools

    # Refresh tokens rotate: the new pair works, the old refresh token doesn't.
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    fresh = client.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
    })
    assert fresh.status_code == 200, fresh.text
    assert rpc(client, fresh.json()["access_token"], "tools/list")["tools"]
    again = client.post("/oauth/token", headers={"Authorization": f"Basic {basic}"}, data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
    })
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    return client_id, secret, fresh.json()["access_token"]


def test_oauth_refuses_what_it_should(client, ids, client_id, secret):
    # A redirect URI that isn't registered never receives anything.
    verifier, challenge = pkce()
    page = authorize(client, ids["admin"], client_id, challenge,
                     redirect_uri="https://evil.example/callback")
    assert page.status_code == 400 and "not registered" in page.text

    # Claude Code's local listener may use any port.
    resp = authorize(client, ids["admin"], client_id, challenge,
                     redirect_uri="http://localhost:53123/callback")
    assert "code" in code_from(resp)

    # Declining sends the refusal back to Claude.
    resp = authorize(client, ids["admin"], client_id, challenge, decision="deny")
    assert code_from(resp)["error"] == "access_denied"

    # Wrong secret; wrong PKCE verifier; a code used twice.
    code = code_from(authorize(client, ids["admin"], client_id, challenge))["code"]
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "code_verifier": verifier, "client_id": client_id, "client_secret": "nalsec_wrong",
    })
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_client"
    code = code_from(authorize(client, ids["admin"], client_id, challenge))["code"]
    bad = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "code_verifier": "not-the-verifier", "client_id": client_id, "client_secret": secret,
    })
    assert bad.json()["error"] == "invalid_grant"
    code = code_from(authorize(client, ids["admin"], client_id, challenge))["code"]
    form = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "code_verifier": verifier, "client_id": client_id, "client_secret": secret,
    }
    assert client.post("/oauth/token", data=form).status_code == 200
    assert client.post("/oauth/token", data=form).json()["error"] == "invalid_grant"


def test_login_returns_to_the_approval_page(client, ids):
    resp = client.get("/oauth/authorize?client_id=x", follow_redirects=False)
    assert resp.status_code == 400  # unknown client: shown, never redirected
    assert safe_next("/oauth/authorize?a=1") == "/oauth/authorize?a=1"
    assert safe_next("//evil.example") is None
    assert safe_next("https://evil.example") is None


def test_players_cannot_connect_yet(client, ids, client_id, secret):
    verifier, challenge = pkce()
    page = authorize(client, ids["player"], client_id, challenge)
    assert page.status_code == 403

    # Nor does a token made behind the approval page's back work.
    db = SessionLocal()
    oc = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).one()
    token = oauth._issue(db, oauth.ACCESS, oc, db.get(User, ids["player"]), oauth.ACCESS_TTL)
    db.commit()
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


def test_pick_sheet_shows_listed_spreads(client, token):
    sheet = ok(client, token, "get_pick_sheet")
    assert sheet["spreads_locked"] is False
    assert sheet["games_without_a_spread"] == []
    games = {g["matchup"]: g for g in sheet["games"]}
    jets = games["NYJ @ NE"]
    assert jets["spread"] == "NYJ -1.5"
    detail = jets["spread_detail"]
    assert (detail["favorite"], detail["underdog"], detail["points"]) == ("NYJ", "NE", 1.5)
    assert (detail["away_line"], detail["home_line"]) == ("NYJ -1.5", "NE +1.5")
    assert detail["set_by"] == "odds feed"
    pickem = games["GB @ CHI"]["spread_detail"]
    assert pickem["favorite"] is None
    assert (pickem["away_line"], pickem["home_line"]) == ("GB PK", "CHI PK")


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


def test_everyones_picks_stay_hidden_until_the_lock(client, token):
    msg = err(client, token, "get_week_picks", week_number=2)
    assert "hidden until the first kickoff" in msg, msg
    msg = err(client, token, "get_rooting_guide", week_number=2)
    assert "hidden until the first kickoff" in msg, msg


def test_week_picks_and_rooting_guide_once_locked(client, token, ids):
    # Ada's week-2 picks are NE 15, SF 16, CHI 14 (from the submit test).
    # Pat takes NE for *more* than Ada did, so NE covering helps Pat more than
    # Ada: she should root against her own pick there.
    db = SessionLocal()
    db.query(Pick).filter(Pick.user_id == ids["player"], Pick.week_id == ids["week2"]).delete()
    season_id = db.get(Week, ids["week2"]).season_id
    ne, sea, chi = ids["w2_games"]
    for game_id, team, pts in [(ne, "NE", 16), (sea, "SEA", 15), (chi, "GB", 14)]:
        db.add(Pick(user_id=ids["player"], game_id=game_id, week_id=ids["week2"],
                    season_id=season_id, picked_team=team, confidence_points=pts))
    db.get(Week, ids["week2"]).is_picks_locked = True
    db.commit()
    db.close()

    picks = ok(client, token, "get_week_picks")
    assert picks["week_number"] == 2
    ne_game = next(g for g in picks["games"] if g["matchup"] == "NYJ @ NE")
    assert [(p["player"], p["team"], p["points"]) for p in ne_game["picks"]] == [
        ("Pat Player", "NE", 16), ("Ada Admin", "NE", 15),
    ]
    assert ne_game["points_on_NE"] == 31 and ne_game["points_on_NYJ"] == 0

    guide = ok(client, token, "get_rooting_guide")
    assert [r["player"] for r in guide["week_rivals"]] == ["Pat Player"]
    assert [r["player"] for r in guide["season_rivals"]] == ["Pat Player"]
    by_game = {g["matchup"]: g for g in guide["games"]}

    ne_row = by_game["NYJ @ NE"]
    assert ne_row["if_covers"]["NE"] == {"you_gain": 15.0, "net_vs": {"Pat Player": -1.0}}
    assert ne_row["root_for_week"] == "NYJ"
    assert ne_row["root_for_season"] == "NYJ"
    assert ne_row["root_against_your_own_pick"] is True

    assert by_game["SF @ SEA"]["root_for_week"] == "SF"
    assert by_game["GB @ CHI"]["root_for_week"] == "CHI"
    assert by_game["GB @ CHI"]["root_against_your_own_pick"] is False


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
        test_discovery_metadata(client, ids)
        client_id, secret, token = test_full_oauth_flow_connects_claude(client, ids)
        test_oauth_refuses_what_it_should(client, ids, client_id, secret)
        test_login_returns_to_the_approval_page(client, ids)
        test_players_cannot_connect_yet(client, ids, client_id, secret)
        test_open_week_results_show_submissions_not_picks(client, token)
        test_week_results_default_to_latest_locked_week(client, token)
        test_season_standings(client, token)
        test_award_standings(client, token)
        test_money_owed(client, token)
        test_pick_sheet_shows_listed_spreads(client, token)
        test_submit_picks_fills_in_and_rearranges(client, token)
        test_invalid_submissions_change_nothing(client, token, ids)
        test_locked_week_refuses_picks(client, token, ids)
        test_everyones_picks_stay_hidden_until_the_lock(client, token)
        test_week_picks_and_rooting_guide_once_locked(client, token, ids)
        test_a_deactivated_owner_loses_access(client, token, ids)
    print("all MCP tests passed")
