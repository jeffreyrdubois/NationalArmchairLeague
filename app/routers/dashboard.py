from app.templates_config import templates
from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Season, Week, Game, Pick, User
from app.auth import get_current_user
from app.services.scoring import get_week_standings, get_season_standings

router = APIRouter()



@router.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    season = db.query(Season).filter(Season.is_active == True).first()
    if not season:
        return templates.TemplateResponse(
            "dashboard/no_season.html", {"request": request, "user": user}
        )

    current_week = (
        db.query(Week)
        .filter(Week.season_id == season.id, Week.is_completed == False)
        .order_by(Week.week_number)
        .first()
    )

    # Season standings
    season_standings = get_season_standings(db, season.id)

    # Current week standings if available
    week_standings = []
    if current_week:
        week_standings = get_week_standings(db, current_week.id)

    # My picks for current week
    my_picks = []
    if current_week:
        my_picks = (
            db.query(Pick)
            .filter(Pick.user_id == user.id, Pick.week_id == current_week.id)
            .all()
        )

    # Current week games
    current_games = []
    if current_week:
        current_games = (
            db.query(Game)
            .filter(Game.week_id == current_week.id)
            .order_by(Game.kickoff_time)
            .all()
        )

    # Weeks for navigation
    all_weeks = (
        db.query(Week)
        .filter(Week.season_id == season.id)
        .order_by(Week.week_number)
        .all()
    )

    return templates.TemplateResponse(
        "dashboard/home.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "current_week": current_week,
            "current_games": current_games,
            "season_standings": season_standings,
            "week_standings": week_standings,
            "my_picks": {p.game_id: p for p in my_picks},
            "all_weeks": all_weeks,
        },
    )


@router.get("/standings", response_class=HTMLResponse)
async def standings_page(
    request: Request,
    season_year: int = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if season_year:
        season = db.query(Season).filter(Season.year == season_year).first()
    else:
        season = db.query(Season).filter(Season.is_active == True).first()

    if not season:
        return templates.TemplateResponse(
            "dashboard/no_season.html", {"request": request, "user": user}
        )

    all_seasons = db.query(Season).order_by(Season.year.desc()).all()
    season_standings = get_season_standings(db, season.id)

    weeks = (
        db.query(Week)
        .filter(Week.season_id == season.id)
        .order_by(Week.week_number)
        .all()
    )

    # Per-week scores for each user (for the chart)
    week_data = []
    for week in weeks:
        ws = get_week_standings(db, week.id)
        week_data.append({
            "week": week,
            "standings": ws,
        })

    return templates.TemplateResponse(
        "dashboard/standings.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "all_seasons": all_seasons,
            "season_standings": season_standings,
            "week_data": week_data,
            "weeks": weeks,
        },
    )


@router.get("/profile/{username}", response_class=HTMLResponse)
async def user_profile(
    request: Request,
    username: str,
    season_year: int = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile_user = db.query(User).filter(User.username == username).first()
    if not profile_user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")

    if season_year:
        season = db.query(Season).filter(Season.year == season_year).first()
    else:
        season = db.query(Season).filter(Season.is_active == True).first()

    all_seasons = db.query(Season).order_by(Season.year.desc()).all()

    picks_by_week = {}
    if season:
        weeks = (
            db.query(Week)
            .filter(Week.season_id == season.id)
            .order_by(Week.week_number)
            .all()
        )
        for week in weeks:
            picks = (
                db.query(Pick)
                .filter(Pick.user_id == profile_user.id, Pick.week_id == week.id)
                .all()
            )
            picks_by_week[week] = picks

    return templates.TemplateResponse(
        "dashboard/profile.html",
        {
            "request": request,
            "user": user,
            "profile_user": profile_user,
            "season": season,
            "all_seasons": all_seasons,
            "picks_by_week": picks_by_week,
        },
    )
