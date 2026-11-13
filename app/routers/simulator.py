"""What-if week standings — flip remaining covers and see the podium."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import Game, Pick, Season, User, Week
from app.routers.picks import game_sort_key
from app.services.simulator import (
    covering_team,
    default_assumptions,
    podium_ranks,
    simulate_week,
)
from app.services.visibility import picks_are_revealed
from app.templates_config import templates
from app.utils import short_labels

router = APIRouter()


def _active_season(db: Session) -> Season | None:
    return db.query(Season).filter(Season.is_active == True).first()  # noqa: E712


def _weeks_for_simulator(db: Session, season: Season) -> list[Week]:
    """Weeks whose picks are revealed — the Simulator needs everyone's picks."""
    weeks = (
        db.query(Week)
        .filter(Week.season_id == season.id)
        .order_by(Week.week_number.desc())
        .all()
    )
    return [w for w in weeks if picks_are_revealed(w)]


def _pick_week(
    weeks: list[Week],
    week_id: int | None,
    unfinished_by_week: dict[int, bool],
) -> Week | None:
    if not weeks:
        return None
    if week_id is not None:
        for week in weeks:
            if week.id == week_id:
                return week
    # Prefer the newest week that still has unfinished games; otherwise the
    # most recent revealed week.
    for week in weeks:
        if unfinished_by_week.get(week.id):
            return week
    return weeks[0]


@router.get("/simulator", response_class=HTMLResponse)
async def simulator(
    request: Request,
    week_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    season = _active_season(db)
    if not season:
        return templates.TemplateResponse(
            "dashboard/no_season.html",
            {"request": request, "user": user},
        )

    weeks = _weeks_for_simulator(db, season)
    # Which weeks still have games to flip — used for the "live" chip and to
    # default the page onto a slate that still has something to simulate.
    unfinished_by_week: dict[int, bool] = {
        week.id: bool(
            db.query(Game.id)
            .filter(Game.week_id == week.id, Game.is_final == False)  # noqa: E712
            .first()
        )
        for week in weeks
    }

    week = _pick_week(weeks, week_id, unfinished_by_week)
    if not week:
        return templates.TemplateResponse(
            "simulator/simulator.html",
            {
                "request": request,
                "user": user,
                "season": season,
                "weeks": [],
                "week": None,
                "games": [],
                "users": [],
                "pick_matrix": {},
                "assumptions": {},
                "standings": [],
                "podium": {},
                "remaining": [],
                "player_labels": {},
                "sim_payload": "null",
            },
        )

    games = (
        db.query(Game)
        .filter(Game.week_id == week.id)
        .order_by(Game.kickoff_time)
        .all()
    )
    games.sort(key=game_sort_key)

    users = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .order_by(User.last_name, User.first_name)
        .all()
    )
    # Viewer first — same frozen-column convention as the all-picks matrix.
    users.sort(key=lambda u: u.id != user.id)

    pick_matrix: dict[int, dict[int, Pick]] = {}
    for pick in db.query(Pick).filter(Pick.week_id == week.id).all():
        pick_matrix.setdefault(pick.user_id, {})[pick.game_id] = pick

    viewer_picks = pick_matrix.get(user.id, {})
    assumptions = default_assumptions(games, viewer_picks)
    standings = simulate_week(games, users, pick_matrix, assumptions)
    podium = podium_ranks(standings)

    remaining = [g for g in games if not g.is_final]
    labels = short_labels(users)

    # Payload the page script uses to recompute when a cover is flipped —
    # plain JSON so the template never has to re-render on each click.
    sim_payload = {
        "viewer_id": user.id,
        "assumptions": {str(gid): hc for gid, hc in assumptions.items()},
        "defaults": {str(gid): hc for gid, hc in assumptions.items()},
        "games": [
            {
                "id": g.id,
                "home": g.home_team,
                "away": g.away_team,
                "is_final": bool(g.is_final),
                "home_covered": g.home_covered if g.is_final else None,
                "spread": g.spread,
            }
            for g in games
        ],
        "users": [
            {
                "id": u.id,
                "label": "You" if u.id == user.id else labels.get(u.id, u.first_name),
                "is_you": u.id == user.id,
            }
            for u in users
        ],
        "picks": {
            str(uid): {
                str(gid): {
                    "team": p.picked_team,
                    "points": int(p.confidence_points or 0),
                }
                for gid, p in by_game.items()
            }
            for uid, by_game in pick_matrix.items()
        },
    }

    # Covering team labels for the initial server render of each toggle.
    assumed_cover = {
        gid: covering_team(
            next(g for g in games if g.id == gid),
            hc,
        )
        for gid, hc in assumptions.items()
    }

    return templates.TemplateResponse(
        "simulator/simulator.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "weeks": weeks,
            "week": week,
            "games": games,
            "users": users,
            "pick_matrix": pick_matrix,
            "assumptions": assumptions,
            "assumed_cover": assumed_cover,
            "standings": standings,
            "podium": podium,
            "remaining": remaining,
            "unfinished_by_week": unfinished_by_week,
            "player_labels": labels,
            "sim_payload": json.dumps(sim_payload),
        },
    )
