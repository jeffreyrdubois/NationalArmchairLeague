"""The league's MCP server, so Claude can read the league and enter picks.

Mounted on the main app at ``/mcp`` (Streamable HTTP, stateless, JSON
responses — nothing to keep alive between calls, and nothing extra for the
reverse proxy to buffer). Every request carries an OAuth access token as
``Authorization: Bearer <token>``; a request without one gets the 401 that
points Claude at the OAuth metadata, and from there at the login-and-approve
page (see app/routers/oauth.py).

Each tool answers as the token's owner and follows the same rules the site
does: nobody's picks are visible before a week locks except the caller's own,
and the money tool is for admins only. The tools reuse the services the pages
are built on, so a number here never disagrees with the one on the site.
"""

import logging
from typing import Any

import anyio

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.database import SessionLocal
from app.models import AppSetting, Game, Pick, Role, Season, Transaction, User, Week
from app.routers.picks import apply_picks, available_points_for
from app.services import payouts, scoring
from app.services.awards import AWARD_REGISTRY, compute_all_awards, rank_award
from app.services.oauth import user_for_access_token
from app.services.scoring import compute_home_covered, get_week_standings
from app.services.visibility import get_submission_status, picks_are_revealed
from app.utils import public_url, to_eastern

INSTRUCTIONS = """\
National Armchair League is a family NFL confidence-pick pool played against
the spread. Each week every player picks the team they think covers in every
game and ranks the games with confidence points (16 for the surest pick; a
short week drops the lowest values). A correct pick earns its confidence
points. Picks lock at the week's first kickoff, and nobody's picks are visible
to anyone else until then.

Weeks are addressed by week_number within a season (1-18 regular season, 19+
playoffs); season_year defaults to the active season. To fill in picks, call
get_pick_sheet first, then submit_picks with a team and a point value per game.

Once a week locks, get_week_picks shows everyone's picks. For "who should I
root for", use get_rooting_guide: the side that helps the caller is not always
the team they picked, since a rival may have more points on it. Times are US
Eastern.
"""


class _QuietStreamClose(logging.Filter):
    """Drop the SDK's "Error in message router" traceback for a closed stream.

    In stateless JSON mode the SDK (1.12.x) closes each request's stream once
    the response is sent, and then logs the router noticing as an error — a
    full traceback on every tool call that is not a fault of any kind. Newer
    SDKs fix it but need a newer uvicorn than this app pins. Anything else the
    SDK logs still gets through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, anyio.ClosedResourceError)


logging.getLogger("mcp.server.streamable_http").addFilter(_QuietStreamClose())

mcp = FastMCP(
    "National Armchair League",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# Transport: authentication in front of the SDK's request handler
# ---------------------------------------------------------------------------

def request_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


class McpEndpoint:
    """ASGI endpoint for ``/mcp``: refuse anything without a valid access
    token before the MCP machinery ever sees it."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        db = SessionLocal()
        try:
            user = user_for_access_token(db, request_token(request))
        finally:
            db.close()
        if not user:
            # RFC 9728: tell the client where to find out how to get a token.
            metadata = public_url(request, "/.well-known/oauth-protected-resource/mcp")
            response = JSONResponse(
                {"error": "invalid_token",
                 "error_description": "Connect through OAuth to get an access token."},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
            )
            await response(scope, receive, send)
            return
        await mcp.session_manager.handle_request(scope, receive, send)


def create_endpoint() -> McpEndpoint:
    mcp.streamable_http_app()  # builds the session manager the endpoint uses
    return McpEndpoint()


def _caller(db: Session, ctx: Context) -> User:
    """The user the calling access token acts as, re-checked on every call."""
    request = ctx.request_context.request
    user = user_for_access_token(db, request_token(request)) if request else None
    if not user:
        raise ToolError("Not authenticated.")
    return user


# ---------------------------------------------------------------------------
# Lookups and formatting
# ---------------------------------------------------------------------------

def _season(db: Session, season_year: int | None) -> Season:
    if season_year:
        season = db.query(Season).filter(Season.year == season_year).first()
        if not season:
            raise ToolError(f"There is no {season_year} season.")
    else:
        season = db.query(Season).filter(Season.is_active == True).first()  # noqa: E712
        if not season:
            raise ToolError("There is no active season.")
    return season


def _weeks(db: Session, season: Season) -> list[Week]:
    return (
        db.query(Week)
        .filter(Week.season_id == season.id)
        .order_by(Week.week_number)
        .all()
    )


def _week_by_number(db: Session, season: Season, week_number: int) -> Week:
    week = (
        db.query(Week)
        .filter(Week.season_id == season.id, Week.week_number == week_number)
        .first()
    )
    if not week:
        raise ToolError(f"The {season.year} season has no week {week_number}.")
    return week


def _open_week(db: Session, season: Season) -> Week | None:
    """The week picks are being made for — the same one the Picks page opens."""
    return (
        db.query(Week)
        .filter(Week.season_id == season.id, Week.is_completed == False)  # noqa: E712
        .order_by(Week.week_number)
        .first()
    )


def _week_label(week: Week) -> str:
    return week.label or f"Week {week.week_number}"


def _eastern(dt) -> str | None:
    local = to_eastern(dt)
    return local.strftime("%a %b %-d, %-I:%M %p ET") if local else None


def _name(user: User | None) -> str:
    return user.full_name if user else "Unknown"


def _line(game: Game) -> str | None:
    """The spread as a bettor reads it: favourite and the points they give."""
    if game.spread is None:
        return None
    if game.spread == 0:
        return "Pick'em"
    favourite = game.home_team if game.spread < 0 else game.away_team
    return f"{favourite} -{abs(game.spread):g}"


def _status(game: Game) -> str:
    if game.is_final:
        return "final"
    if game.is_underway:
        return "in_progress"
    return "upcoming"


def _covering_team(game: Game) -> str | None:
    if game.home_covered is None:
        return None
    return game.home_team if game.home_covered else game.away_team


def _game(game: Game) -> dict[str, Any]:
    row: dict[str, Any] = {
        "game_id": game.id,
        "matchup": f"{game.away_team} @ {game.home_team}",
        "away_team": game.away_team,
        "away_team_name": game.away_team_name,
        "home_team": game.home_team,
        "home_team_name": game.home_team_name,
        "kickoff": _eastern(game.kickoff_time),
        "spread": _line(game),
        "status": _status(game),
    }
    if game.home_score is not None and game.away_score is not None:
        row["score"] = (
            f"{game.away_team} {game.away_score} - {game.home_team} {game.home_score}"
        )
    if game.is_underway and game.quarter:
        row["clock"] = " ".join(p for p in (game.quarter, game.time_remaining) if p)
    if game.is_final:
        row["covered"] = _covering_team(game)
    return row


def _ranked(rows: list[dict], key: str) -> list[tuple[int, dict]]:
    """Attach finishing places to a sorted list, with ties sharing a place."""
    out = []
    place = 0
    last = object()
    for i, row in enumerate(rows, start=1):
        if row[key] != last:
            place = i
            last = row[key]
        out.append((place, row))
    return out


def _money(amount: float) -> float:
    return round(float(amount or 0), 2)


def _week_games(db: Session, week: Week) -> list[Game]:
    return (
        db.query(Game)
        .filter(Game.week_id == week.id)
        .order_by(Game.kickoff_time)
        .all()
    )


# ---------------------------------------------------------------------------
# Tools: reading the league
# ---------------------------------------------------------------------------

@mcp.tool()
def get_week_results(
    ctx: Context,
    week_number: int | None = None,
    season_year: int | None = None,
) -> dict[str, Any]:
    """Results for one week: every game's score and who covered, your pick on
    each, and the week's leaderboard.

    Defaults to the most recent week whose picks have locked. Before a week
    locks nobody's picks can be seen but your own, so an unlocked week shows
    who has submitted instead of a leaderboard.
    """
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        season = _season(db, season_year)
        if week_number is not None:
            week = _week_by_number(db, season, week_number)
        else:
            locked = [w for w in _weeks(db, season) if picks_are_revealed(w)]
            week = locked[-1] if locked else _open_week(db, season)
            if not week:
                raise ToolError(f"The {season.year} season has no weeks yet.")

        revealed = picks_are_revealed(week)
        mine = {
            p.game_id: p
            for p in db.query(Pick).filter(
                Pick.week_id == week.id, Pick.user_id == user.id
            )
        }
        games = []
        for game in _week_games(db, week):
            row = _game(game)
            pick = mine.get(game.id)
            if pick:
                row["your_pick"] = {
                    "team": pick.picked_team,
                    "points": pick.confidence_points,
                    "result": (
                        "pending" if pick.is_correct is None
                        else "correct" if pick.is_correct else "wrong"
                    ),
                    "points_earned": pick.points_earned,
                }
            games.append(row)

        result: dict[str, Any] = {
            "season": season.year,
            "week_number": week.week_number,
            "week": _week_label(week),
            "picks_locked": revealed,
            "week_completed": bool(week.is_completed),
            "games": games,
        }
        if revealed:
            standings = get_week_standings(db, week.id)
            result["leaderboard"] = [
                {
                    "place": place,
                    "player": _name(row["user"]),
                    "is_you": bool(row["user"] and row["user"].id == user.id),
                    "points": row["total"],
                    "correct": row["correct"],
                    "wrong": row["wrong"],
                    "pending": row["pending"],
                    "max_possible": row["potential"],
                }
                for place, row in _ranked(standings, "total")
            ]
        else:
            result["picks_lock_at"] = _eastern(week.first_kickoff)
            result["submissions"] = [
                {
                    "player": _name(row["user"]),
                    "picks_made": row["picks_made"],
                    "of": row["n_games"],
                    "complete": row["is_complete"],
                }
                for row in get_submission_status(db, week)
            ]
        return result
    finally:
        db.close()


@mcp.tool()
def get_season_standings(
    ctx: Context,
    season_year: int | None = None,
) -> dict[str, Any]:
    """The overall season leaderboard: total points, correct and wrong picks,
    how far each player trails the leader, and how many weeks each has won."""
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        season = _season(db, season_year)
        standings = scoring.get_season_standings(db, season.id)

        # A week is "won" once it is fully played; a tie for the top shares it.
        week_wins: dict[int, int] = {}
        weeks_scored = 0
        for week in _weeks(db, season):
            games = _week_games(db, week)
            if not games or not all(g.is_final for g in games):
                continue
            weeks_scored += 1
            week_rows = get_week_standings(db, week.id)
            if not week_rows:
                continue
            best = week_rows[0]["total"]
            for row in week_rows:
                if row["total"] == best and row["user"]:
                    week_wins[row["user"].id] = week_wins.get(row["user"].id, 0) + 1

        leader = standings[0]["total"] if standings else 0
        return {
            "season": season.year,
            "weeks_fully_scored": weeks_scored,
            "standings": [
                {
                    "place": place,
                    "player": _name(row["user"]),
                    "is_you": bool(row["user"] and row["user"].id == user.id),
                    "points": row["total"],
                    "behind_leader": leader - row["total"],
                    "correct": row["correct"],
                    "wrong": row["wrong"],
                    "pending": row["pending"],
                    "weeks_won": week_wins.get(row["user"].id, 0) if row["user"] else 0,
                }
                for place, row in _ranked(standings, "total")
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_award_standings(
    ctx: Context,
    award: str | None = None,
    top: int = 5,
    season_year: int | None = None,
) -> dict[str, Any]:
    """How the season awards are ranked: each award's rules, its prize, the
    leaders (ties included, so there may be more than ``top``), and where you
    stand. Pass ``award`` (an id or part of a name, e.g. "contrarian") for one
    award only."""
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        season = _season(db, season_year)
        configs = [cfg for cfg in AWARD_REGISTRY if cfg.enabled]
        if award:
            needle = award.strip().lower().replace(" ", "_")
            configs = [
                cfg for cfg in configs
                if needle == cfg.id or needle in cfg.id or needle in cfg.name.lower().replace(" ", "_")
            ]
            if not configs:
                names = ", ".join(c.name for c in AWARD_REGISTRY if c.enabled)
                raise ToolError(f"No award matches {award!r}. Awards: {names}.")

        scores = compute_all_awards(db, season.id)
        users_by_id = {
            u.id: u for u in db.query(User).filter(User.is_active == True)  # noqa: E712
        }
        plan = payouts.load_plan(db, season.id)
        top = max(1, top)

        awards = []
        for cfg in configs:
            ranking = rank_award(scores.get(cfg.id, {}), users_by_id, cfg.win_condition)
            yours = next((r for r in ranking if r["user"].id == user.id), None)
            awards.append({
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "winner_has": cfg.win_condition,
                "prize": _money(plan.awards.get(cfg.id, 0)) or None,
                "leaders": [
                    {"place": r["rank"], "player": _name(r["user"]), "score": r["score"]}
                    for r in ranking if r["rank"] <= top
                ],
                "you": (
                    {"place": yours["rank"], "score": yours["score"]} if yours else None
                ),
            })
        return {"season": season.year, "awards": awards}
    finally:
        db.close()


@mcp.tool()
def get_money_owed(
    ctx: Context,
    include_settled: bool = False,
) -> dict[str, Any]:
    """Who is owed money and who owes it (admins only).

    ``prizes_owed``: prize money each player has won under the season's payout
    plan, less what has already been logged as paid to them, with the prizes
    behind each figure. Season-end and award prizes are projections until the
    season is over. ``entry_fees_outstanding``: players who have not paid the
    full entry fee. Settled players are left out unless ``include_settled``.
    """
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        if user.role != Role.admin:
            raise ToolError("League money is visible to admins only.")

        season = db.query(Season).filter(Season.is_active == True).first()  # noqa: E712
        players = (
            db.query(User)
            .filter(User.is_active == True)  # noqa: E712
            .order_by(User.last_name, User.first_name)
            .all()
        )
        plan, report, ledger = payouts.league_ledger(db, season, players)

        prizes = []
        for row in ledger:
            if not include_settled and row["owed"] <= 0 and row["projected"] <= 0:
                continue
            prizes.append({
                "player": _name(row["user"]),
                "owed_now": row["owed"],
                "earned": row["earned"],
                "already_paid": row["paid"],
                "projected_if_season_ended_today": row["projected"],
                "prizes": [
                    {
                        "for": line.label,
                        "place": line.place,
                        "amount": line.amount,
                        "status": "earned" if line.earned else "projected",
                    }
                    for line in row["lines"]
                ],
            })

        fee_row = db.query(AppSetting).filter(AppSetting.key == "entry_fee").first()
        entry_fee = float(fee_row.value) if fee_row and fee_row.value else 0.0
        paid_in: dict[int, float] = {}
        for txn in db.query(Transaction).filter(Transaction.direction == "in"):
            paid_in[txn.user_id] = paid_in.get(txn.user_id, 0) + txn.amount
        fees = []
        for player in players:
            balance = _money(entry_fee - paid_in.get(player.id, 0))
            if balance > 0 or include_settled:
                fees.append({
                    "player": _name(player),
                    "paid": _money(paid_in.get(player.id, 0)),
                    "still_owes": max(balance, 0.0),
                })

        return {
            "season": season.year if season else None,
            "payout_plan_configured": plan.is_configured,
            "season_complete": report.season_complete,
            "weeks_paid_out": report.weeks_paid,
            "total_prizes_owed_now": _money(
                sum(row["owed"] for row in ledger if row["owed"] > 0)
            ),
            "prizes_owed": prizes,
            "entry_fee": _money(entry_fee),
            "total_entry_fees_outstanding": _money(sum(f["still_owes"] for f in fees)),
            "entry_fees_outstanding": fees,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tools: everyone's picks, and who to root for
# ---------------------------------------------------------------------------

def _revealed_week(db: Session, season: Season, week_number: int | None) -> Week:
    """A week whose picks everyone may see — refusing one that is still secret."""
    if week_number is not None:
        week = _week_by_number(db, season, week_number)
    else:
        locked = [w for w in _weeks(db, season) if picks_are_revealed(w)]
        if not locked:
            raise ToolError("No week's picks have been revealed yet.")
        week = locked[-1]
    if not picks_are_revealed(week):
        raise ToolError(
            f"{_week_label(week)} picks stay hidden until the first kickoff "
            f"({_eastern(week.first_kickoff) or 'time not set'}). "
            "get_week_results shows who has submitted."
        )
    return week


def _currently_covering(game: Game) -> str | None:
    """Who would cover if the game ended on the current score."""
    if game.is_final:
        return _covering_team(game)
    if not game.is_underway or game.spread is None:
        return None
    if game.home_score is None or game.away_score is None:
        return None
    covered = compute_home_covered(game.home_score, game.away_score, game.spread)
    return game.home_team if covered else game.away_team


@mcp.tool()
def get_week_picks(
    ctx: Context,
    week_number: int | None = None,
    season_year: int | None = None,
) -> dict[str, Any]:
    """Everyone's picks for a week, game by game, with each pick's result.

    Only for weeks whose picks have locked (the first kickoff) — before that
    they are secret, exactly as on the site. Defaults to the most recent
    locked week.
    """
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        season = _season(db, season_year)
        week = _revealed_week(db, season, week_number)

        by_game: dict[int, list[Pick]] = {}
        for pick in db.query(Pick).filter(Pick.week_id == week.id):
            by_game.setdefault(pick.game_id, []).append(pick)

        games = []
        for game in _week_games(db, week):
            row = _game(game)
            leaning = _currently_covering(game)
            if leaning and not game.is_final:
                row["currently_covering"] = leaning
            picks = sorted(
                by_game.get(game.id, []),
                key=lambda p: (-p.confidence_points, _name(p.user)),
            )
            row["picks"] = [
                {
                    "player": _name(p.user),
                    "is_you": p.user_id == user.id,
                    "team": p.picked_team,
                    "points": p.confidence_points,
                    "result": (
                        "pending" if p.is_correct is None
                        else "correct" if p.is_correct else "wrong"
                    ),
                }
                for p in picks
            ]
            for team in (game.away_team, game.home_team):
                row[f"points_on_{team}"] = sum(
                    p.confidence_points for p in picks if p.picked_team == team
                )
            games.append(row)

        return {
            "season": season.year,
            "week_number": week.week_number,
            "week": _week_label(week),
            "games": games,
        }
    finally:
        db.close()


def _outcome_net(deltas: dict[int, float], me: int, rivals: list[int]) -> float:
    return sum(deltas.get(me, 0) - deltas.get(r, 0) for r in rivals)


def _pick_side(nets: dict[str, float]) -> str:
    (a, na), (b, nb) = nets.items()
    if na == nb:
        return "either"
    return a if na > nb else b


@mcp.tool()
def get_rooting_guide(
    ctx: Context,
    week_number: int | None = None,
) -> dict[str, Any]:
    """Which team to root for in each unfinished game of a locked week.

    Your pick is not always the side that helps you: if you took a team for 3
    points and the people you are racing took it for 14, their cover hurts you.
    For every game still to be decided this weighs both outcomes by what each
    player gains, against the players you are actually racing:

    - week: everyone whose race with you for the week is still live (either of
      you can still finish ahead on points remaining);
    - season: the nearest player(s) ahead of and behind you in the standings.

    ``net_vs`` gives your gain minus theirs for each outcome, per player, so
    any other rival can be weighed too.
    """
    db = SessionLocal()
    try:
        me = _caller(db, ctx)
        season = _season(db, None)
        week = _revealed_week(db, season, week_number)

        week_rows = {r["user"].id: r for r in get_week_standings(db, week.id) if r["user"]}
        season_rows = [r for r in scoring.get_season_standings(db, season.id) if r["user"]]
        names = {uid: _name(r["user"]) for uid, r in week_rows.items()}
        names.update({r["user"].id: _name(r["user"]) for r in season_rows})
        if me.id not in week_rows:
            raise ToolError(f"You have no picks in {_week_label(week)}.")

        # --- the week race: who can still finish either side of you ---
        mine = week_rows[me.id]
        week_rivals = [
            uid for uid, r in week_rows.items()
            if uid != me.id
            and r["potential"] >= mine["total"]
            and mine["potential"] >= r["total"]
        ]

        # --- the season race: your nearest neighbours in the standings ---
        season_total = {r["user"].id: r["total"] for r in season_rows}
        my_season = season_total.get(me.id, 0)
        ahead = [t for uid, t in season_total.items() if uid != me.id and t >= my_season]
        behind = [t for uid, t in season_total.items() if uid != me.id and t < my_season]
        season_rivals = [
            uid for uid, t in season_total.items()
            if uid != me.id and (
                (ahead and t == min(ahead)) or (behind and t == max(behind))
            )
        ]

        picks_by_game: dict[int, dict[int, Pick]] = {}
        for pick in db.query(Pick).filter(Pick.week_id == week.id):
            picks_by_game.setdefault(pick.game_id, {})[pick.user_id] = pick

        games = []
        for game in _week_games(db, week):
            if game.is_final:
                continue
            picks = picks_by_game.get(game.id, {})
            outcomes = {}
            deltas_by_team = {}
            for team in (game.away_team, game.home_team):
                deltas = {
                    uid: float(p.confidence_points) if p.picked_team == team else 0.0
                    for uid, p in picks.items()
                }
                deltas_by_team[team] = deltas
                outcomes[team] = {
                    "you_gain": deltas.get(me.id, 0.0),
                    "net_vs": {
                        names.get(uid, "Unknown"): deltas.get(me.id, 0.0) - d
                        for uid, d in sorted(deltas.items())
                        if uid != me.id
                    },
                }
            week_net = {
                t: _outcome_net(d, me.id, week_rivals) for t, d in deltas_by_team.items()
            }
            season_net = {
                t: _outcome_net(d, me.id, season_rivals) for t, d in deltas_by_team.items()
            }
            your = picks.get(me.id)
            row = _game(game)
            leaning = _currently_covering(game)
            if leaning:
                row["currently_covering"] = leaning
            row.update({
                "your_pick": (
                    {"team": your.picked_team, "points": your.confidence_points}
                    if your else None
                ),
                "if_covers": outcomes,
                "root_for_week": (
                    _pick_side(week_net) if week_rivals
                    else (your.picked_team if your else "either")
                ),
                "week_net_by_outcome": week_net,
                "root_for_season": _pick_side(season_net) if season_rivals else "either",
                "season_net_by_outcome": season_net,
            })
            row["root_against_your_own_pick"] = bool(
                your and row["root_for_week"] not in (your.picked_team, "either")
            )
            games.append(row)

        return {
            "season": season.year,
            "week_number": week.week_number,
            "week": _week_label(week),
            "you": {
                "week_points": mine["total"],
                "week_max_possible": mine["potential"],
                "season_points": my_season,
            },
            "week_rivals": [
                {
                    "player": names[uid],
                    "points": week_rows[uid]["total"],
                    "max_possible": week_rows[uid]["potential"],
                }
                for uid in week_rivals
            ],
            "season_rivals": [
                {"player": names[uid], "season_points": season_total[uid]}
                for uid in season_rivals
            ],
            "games": games,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tools: making picks
# ---------------------------------------------------------------------------

def _pick_sheet(db: Session, week: Week, user: User) -> dict[str, Any]:
    games = _week_games(db, week)
    points = available_points_for(len(games))
    mine = {
        p.game_id: p
        for p in db.query(Pick).filter(Pick.week_id == week.id, Pick.user_id == user.id)
    }
    rows = []
    for game in games:
        row = _game(game)
        pick = mine.get(game.id)
        row["your_pick"] = (
            {"team": pick.picked_team, "points": pick.confidence_points} if pick else None
        )
        rows.append(row)
    used = {p.confidence_points for p in mine.values()}
    return {
        "season": week.season.year,
        "week_number": week.week_number,
        "week": _week_label(week),
        "picks_locked": bool(week.is_picks_locked),
        "picks_lock_at": _eastern(week.first_kickoff),
        "point_values": points,
        "unused_point_values": [p for p in points if p not in used],
        "games_without_a_pick": [r["matchup"] for r in rows if not r["your_pick"]],
        "complete": bool(games) and len(mine) >= len(games),
        "games": rows,
    }


def _pick_week(db: Session, week_number: int | None) -> Week:
    season = _season(db, None)
    if week_number is not None:
        return _week_by_number(db, season, week_number)
    week = _open_week(db, season)
    if not week:
        raise ToolError("No week is open for picks right now.")
    return week


@mcp.tool()
def get_pick_sheet(
    ctx: Context,
    week_number: int | None = None,
) -> dict[str, Any]:
    """Your pick sheet for a week of the active season (default: the week open
    for picks): every game with its spread and kickoff, the pick you have in
    for it, the point values the week uses and which are still unassigned."""
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        return _pick_sheet(db, _pick_week(db, week_number), user)
    finally:
        db.close()


class PickInput(BaseModel):
    team: str = Field(
        description="The team you pick to cover: abbreviation (\"KC\") or name (\"Chiefs\")."
    )
    points: int = Field(description="Confidence points to put on this game.")


def _match_game(games: list[Game], team: str) -> tuple[Game, str]:
    """Find the game a team plays in this week, by abbreviation or name."""
    wanted = team.strip().lower()
    for game in games:
        for abbr in (game.home_team, game.away_team):
            if abbr.lower() == wanted:
                return game, abbr
    hits = []
    for game in games:
        for abbr, name in (
            (game.home_team, game.home_team_name),
            (game.away_team, game.away_team_name),
        ):
            if name and wanted in name.lower():
                hits.append((game, abbr))
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(f"{team!r} matches more than one team: "
                        + ", ".join(abbr for _, abbr in hits) + ".")
    raise ToolError(f"No team matching {team!r} plays this week.")


@mcp.tool()
def submit_picks(
    ctx: Context,
    picks: list[PickInput],
    week_number: int | None = None,
) -> dict[str, Any]:
    """Enter or change your picks for a week of the active season (default:
    the week open for picks).

    Each pick names the team you expect to cover and its confidence points.
    Games you leave out keep the pick you already have, so this can fill in
    just the missing games or move a few values around; once everything is
    applied every point value may be used only once. Nothing is saved unless
    the whole submission is valid. Returns your updated pick sheet.
    """
    db = SessionLocal()
    try:
        user = _caller(db, ctx)
        week = _pick_week(db, week_number)
        if week.is_picks_locked:
            raise ToolError(f"Picks are locked for {_week_label(week)}.")
        games = _week_games(db, week)
        if not games:
            raise ToolError(f"{_week_label(week)} has no games yet.")
        if not picks:
            raise ToolError("No picks were given.")
        allowed = set(available_points_for(len(games)))

        changes: dict[int, tuple[int, str]] = {}
        for pick in picks:
            game, abbr = _match_game(games, pick.team)
            if game.id in changes:
                raise ToolError(
                    f"{game.away_team} @ {game.home_team} was picked more than once."
                )
            if pick.points not in allowed:
                raise ToolError(
                    f"{pick.points} is not a point value this week "
                    f"({min(allowed)}-{max(allowed)})."
                )
            changes[game.id] = (pick.points, abbr)

        # Check the slate as it will stand once these are applied, so a clash
        # with a pick being kept is reported by name rather than as a failed
        # save.
        existing = {
            p.game_id: p
            for p in db.query(Pick).filter(Pick.week_id == week.id, Pick.user_id == user.id)
        }
        final: dict[int, int] = {gid: p.confidence_points for gid, p in existing.items()}
        final.update({gid: pts for gid, (pts, _) in changes.items()})
        by_game = {g.id: g for g in games}
        seen: dict[int, int] = {}
        for gid, pts in final.items():
            if pts in seen:
                a, b = by_game[seen[pts]], by_game[gid]
                raise ToolError(
                    f"{pts} points would be on both {a.away_team} @ {a.home_team} "
                    f"and {b.away_team} @ {b.home_team}. Move one of them in the "
                    "same submission."
                )
            seen[pts] = gid

        try:
            apply_picks(db, user.id, week, changes)
        except HTTPException as exc:
            db.rollback()
            raise ToolError(str(exc.detail))
        db.commit()
        return _pick_sheet(db, week, user)
    finally:
        db.close()
