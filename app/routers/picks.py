from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.orm import Session
from typing import Annotated
from app.database import get_db
from app.models import Season, Week, Game, Pick, User, AuditLog
from app.auth import get_current_user, require_user

router = APIRouter()



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
    # Available points: skip lowest values for short weeks
    # 16 games -> 1-16, 15 games -> 2-16, 14 games -> 3-16
    max_full = 16
    available_points = list(range(max_full - n_games + 1, max_full + 1))

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
    n_games = len(games)
    max_full = 16
    available_points = set(range(max_full - n_games + 1, max_full + 1))

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

    # Save picks
    for game_id, (points, team) in new_picks.items():
        existing = db.query(Pick).filter(
            Pick.user_id == user.id, Pick.game_id == game_id
        ).first()
        if existing:
            existing.confidence_points = points
            existing.picked_team = team
            existing.is_correct = None
            existing.points_earned = None
        else:
            pick = Pick(
                user_id=user.id,
                game_id=game_id,
                week_id=week_id,
                season_id=week.season_id,
                picked_team=team,
                confidence_points=points,
            )
            db.add(pick)

    db.commit()
    return RedirectResponse(url=f"/picks?week_id={week_id}&saved=1", status_code=303)


@router.get("/picks/week/{week_id}/all", response_class=HTMLResponse)
async def all_picks_for_week(
    request: Request,
    week_id: int,
    db: Session = Depends(get_db),
):
    """View all users' picks for a week — only visible after picks are locked."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        raise HTTPException(status_code=404, detail="Week not found")

    from app.models import Role
    if not week.is_picks_locked and user.role != Role.admin:
        raise HTTPException(status_code=403, detail="Picks are not yet revealed")

    games = (
        db.query(Game)
        .filter(Game.week_id == week_id)
        .order_by(Game.kickoff_time)
        .all()
    )
    users = db.query(User).filter(User.is_active == True).all()
    all_picks = db.query(Pick).filter(Pick.week_id == week_id).all()

    # Build matrix: {user_id: {game_id: pick}}
    pick_matrix = {}
    for pick in all_picks:
        pick_matrix.setdefault(pick.user_id, {})[pick.game_id] = pick

    from app.services.scoring import get_week_standings
    standings = get_week_standings(db, week_id)

    return templates.TemplateResponse(
        "picks/all_picks.html",
        {
            "request": request,
            "user": user,
            "week": week,
            "games": games,
            "users": users,
            "pick_matrix": pick_matrix,
            "standings": standings,
        },
    )
