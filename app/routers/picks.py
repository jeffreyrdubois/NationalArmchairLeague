from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Annotated
from app.database import get_db
from app.models import Season, Week, Game, Pick, User, AuditLog
from app.auth import get_current_user, require_user
from app.services.visibility import get_submission_status, picks_are_revealed

router = APIRouter()


def game_sort_key(game: Game):
    """Order games being played first, then upcoming, then finished.

    Only the bucket is returned: callers sort a list already in kickoff order,
    and Python's sort is stable, so the schedule still reads in order inside
    each bucket.
    """
    if game.is_underway:
        return 0
    if game.is_final:
        return 2
    return 1


# A full slate of 16 games is worth 1-16. A short week drops the lowest values
# rather than the highest, so the top pick is always worth 16:
# 15 games -> 2-16, 14 games -> 3-16.
MAX_CONFIDENCE_POINTS = 16


def available_points_for(n_games: int) -> list[int]:
    """The confidence values a week with ``n_games`` games hands out."""
    return list(range(MAX_CONFIDENCE_POINTS - n_games + 1, MAX_CONFIDENCE_POINTS + 1))


def get_active_season_week(db: Session):
    season = db.query(Season).filter(Season.is_active == True).first()
    if not season:
        return None, None
    week = (
        db.query(Week)
        .filter(Week.season_id == season.id, Week.is_completed == False)
        .order_by(Week.week_number)
        .first()
    )
    return season, week


def build_pick_context(db: Session, week: Week, user: User, admin_user_id: int = None):
    """Build context for the picks page."""
    games = (
        db.query(Game)
        .filter(Game.week_id == week.id)
        .order_by(Game.kickoff_time)
        .all()
    )
    n_games = len(games)
    available_points = available_points_for(n_games)

    target_user_id = admin_user_id or user.id
    existing_picks = {
        p.game_id: p
        for p in db.query(Pick).filter(
            Pick.user_id == target_user_id,
            Pick.week_id == week.id,
        ).all()
    }

    used_points = {p.confidence_points for p in existing_picks.values()}

    return {
        "games": games,
        "week": week,
        "n_games": n_games,
        "available_points": available_points,
        "existing_picks": existing_picks,
        "used_points": used_points,
    }


def apply_picks(db: Session, user_id: int, week: Week, new_picks: dict):
    """Write ``{game_id: (confidence_points, picked_team)}`` for one user.

    Confidence points are unique per user per week at the database level, so
    rewriting picks in place breaks the moment two games trade point values:
    the first UPDATE collides with the row still holding the value the other
    game is moving to. Park every existing pick on a temporary out-of-range
    value first, then write the real ones. Picks the caller left out (a
    partial admin edit) keep the values they already had.

    Returns ``{game_id: (old_points, old_team)}`` for the picks that already
    existed, so callers can describe what changed.
    """
    existing = {
        p.game_id: p
        for p in db.query(Pick).filter(
            Pick.user_id == user_id,
            Pick.week_id == week.id,
        ).all()
    }
    previous = {
        game_id: (pick.confidence_points, pick.picked_team)
        for game_id, pick in existing.items()
    }

    for offset, pick in enumerate(existing.values(), start=1):
        pick.confidence_points = -offset
    db.flush()

    for game_id, (points, team) in new_picks.items():
        pick = existing.get(game_id)
        if pick:
            pick.confidence_points = points
            pick.picked_team = team
            pick.is_correct = None
            pick.points_earned = None
        else:
            db.add(
                Pick(
                    user_id=user_id,
                    game_id=game_id,
                    week_id=week.id,
                    season_id=week.season_id,
                    picked_team=team,
                    confidence_points=points,
                )
            )

    # Put back anything that wasn't part of this save.
    assigned = {points for points, _ in new_picks.values()}
    for game_id, pick in existing.items():
        if game_id in new_picks:
            continue
        old_points = previous[game_id][0]
        if old_points in assigned:
            raise HTTPException(
                status_code=400,
                detail=f"{old_points} points is already used on another game this week",
            )
        pick.confidence_points = old_points

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Each point value can only be used once per week",
        )

    return previous


@router.get("/picks", response_class=HTMLResponse)
async def picks_page(
    request: Request,
    week_id: int = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    season, current_week = get_active_season_week(db)
    if not season:
        return templates.TemplateResponse(
            "picks/no_season.html", {"request": request, "user": user}
        )

    if week_id:
        week = db.query(Week).filter(Week.id == week_id, Week.season_id == season.id).first()
        if not week:
            week = current_week
    else:
        week = current_week

    if not week:
        return templates.TemplateResponse(
            "picks/no_week.html", {"request": request, "user": user, "season": season}
        )

    ctx = build_pick_context(db, week, user)
    ctx.update({
        "request": request,
        "user": user,
        "season": season,
        "current_week": current_week,
    })
    return templates.TemplateResponse("picks/picks.html", ctx)


@router.post("/picks/save")
async def save_picks(
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    week_id = int(form.get("week_id"))
    week = db.query(Week).filter(Week.id == week_id).first()

    if not week:
        raise HTTPException(status_code=404, detail="Week not found")
    if week.is_picks_locked:
        raise HTTPException(status_code=400, detail="Picks are locked for this week")

    games = db.query(Game).filter(Game.week_id == week_id).all()
    available_points = set(available_points_for(len(games)))

    # Parse picks from form: format is "game_{game_id}_team" and "game_{game_id}_points"
    new_picks = {}
    for game in games:
        team_key = f"game_{game.id}_team"
        points_key = f"game_{game.id}_points"
        picked_team = form.get(team_key)
        points_str = form.get(points_key)

        if not picked_team or not points_str:
            raise HTTPException(status_code=400, detail=f"Missing pick for game {game.id}")

        try:
            points = int(points_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid points value")

        if picked_team not in (game.home_team, game.away_team):
            raise HTTPException(status_code=400, detail="Invalid team selection")
        if points not in available_points:
            raise HTTPException(status_code=400, detail=f"Invalid points value: {points}")
        if points in {p for g_id, (p, _) in new_picks.items()}:
            raise HTTPException(status_code=400, detail="Duplicate point values not allowed")

        new_picks[game.id] = (points, picked_team)

    # Validate no duplicate point assignments
    point_values = [v[0] for v in new_picks.values()]
    if len(point_values) != len(set(point_values)):
        raise HTTPException(status_code=400, detail="Each point value can only be used once")

    apply_picks(db, user.id, week, new_picks)
    db.commit()
    return RedirectResponse(url=f"/picks?week_id={week_id}&saved=1", status_code=303)


@router.get("/picks/week/{week_id}/all", response_class=HTMLResponse)
async def all_picks_for_week(
    request: Request,
    week_id: int,
    db: Session = Depends(get_db),
):
    """Everyone's picks for a week — revealed only once the week is locked.

    Before the lock this is a submission board instead: who has their picks in
    and who still owes them, with no pick content for anyone (an admin
    included). The picks themselves are not even loaded, so there is nothing
    for the page to leak.
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        raise HTTPException(status_code=404, detail="Week not found")

    revealed = picks_are_revealed(week)

    games = (
        db.query(Game)
        .filter(Game.week_id == week_id)
        .order_by(Game.kickoff_time)
        .all()
    )
    # Games in play come first, then the ones still to kick off, then the
    # finals — the rows worth watching sit at the top of the matrix instead of
    # wherever the schedule happens to put them. The sort is stable, so
    # kickoff order survives inside each group.
    games.sort(key=game_sort_key)

    # The viewer's own column comes first so it sits inside the frozen block
    # of the pick matrix — the point of comparing is comparing against yours.
    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.last_name, User.first_name)
        .all()
    )
    users.sort(key=lambda u: u.id != user.id)

    pick_matrix = {}
    standings = []
    root_for_game = {}
    if revealed:
        # Build matrix: {user_id: {game_id: pick}}
        for pick in db.query(Pick).filter(Pick.week_id == week_id).all():
            pick_matrix.setdefault(pick.user_id, {})[pick.game_id] = pick

        from app.services.scoring import get_week_standings
        standings = get_week_standings(db, week_id)

        # The side of each unfinished game that helps the viewer most — not
        # always their own pick, when a rival has more points on it.
        from app.services.rooting import root_for, rooting_guide
        guide = rooting_guide(db, week, user.id)
        root_for_game = {g.id: root_for(guide, g) for g in games}

    submission_status = get_submission_status(db, week)

    return templates.TemplateResponse(
        "picks/all_picks.html",
        {
            "request": request,
            "user": user,
            "week": week,
            "games": games,
            "users": users,
            "revealed": revealed,
            "pick_matrix": pick_matrix,
            "standings": standings,
            "root_for_game": root_for_game,
            "submission_status": submission_status,
            "submitted_count": sum(1 for r in submission_status if r["is_complete"]),
        },
    )
