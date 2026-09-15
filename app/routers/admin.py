from app.templates_config import templates
"""
Admin and Contributor routes.
- Contributors: update spreads and scores manually
- Admins: all of the above + manage users, edit any pick, manage seasons/weeks
"""
import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import (
    Season, Week, Game, Pick, User, Role, AuditLog, ScoreSource, SpreadSource,
    PushSubscription, Transaction, AppSetting, Invite,
    generate_invite_code,
)
from app.auth import get_current_user, require_contributor, require_admin, hash_password
from app.services import espn, github_issues, payouts
from app.services import docker_api, registry, selfupdate
from app.services.awards import AWARD_REGISTRY
from app.services.scoring import unscore_game, update_game_results
from app.utils import eastern_to_utc, to_eastern

router = APIRouter(prefix="/admin")

TEST_SEASON_YEAR = 9999

_OPEN_GAMES = [
    ("KC",  "Kansas City Chiefs",      "LV",  "Las Vegas Raiders",        -7.5),
    ("DAL", "Dallas Cowboys",          "PHI", "Philadelphia Eagles",       3.5),
    ("BUF", "Buffalo Bills",           "MIA", "Miami Dolphins",           -3.0),
    ("SF",  "San Francisco 49ers",     "LAR", "Los Angeles Rams",         -1.5),
    ("DET", "Detroit Lions",           "GB",  "Green Bay Packers",         2.5),
    ("NYG", "New York Giants",         "WAS", "Washington Commanders",     1.0),
    ("CLE", "Cleveland Browns",        "PIT", "Pittsburgh Steelers",       3.5),
    ("SEA", "Seattle Seahawks",        "ARI", "Arizona Cardinals",        -4.5),
]

# (away, away_name, home, home_name, spread, away_score, home_score)
_DONE_GAMES = [
    ("NE",  "New England Patriots",    "NYJ", "New York Jets",            -2.5,  17, 24),
    ("BAL", "Baltimore Ravens",        "CIN", "Cincinnati Bengals",       -5.5,  27, 20),
    ("MIN", "Minnesota Vikings",       "CHI", "Chicago Bears",            -3.5,  31, 17),
    ("NO",  "New Orleans Saints",      "ATL", "Atlanta Falcons",           1.5,  14, 28),
    ("TEN", "Tennessee Titans",        "IND", "Indianapolis Colts",        2.0,  20, 17),
    ("JAX", "Jacksonville Jaguars",    "HOU", "Houston Texans",            4.0,  10, 23),
    ("DEN", "Denver Broncos",          "LAC", "Los Angeles Chargers",     -1.0,  21, 14),
    ("TB",  "Tampa Bay Buccaneers",    "CAR", "Carolina Panthers",        -6.5,  34,  7),
]

ESPN_CDN = "https://a.espncdn.com/i/teamlogos/nfl/500"


@router.post("/test-season/create")
async def create_test_season(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    # Remove any existing test season
    existing = db.query(Season).filter(Season.year == TEST_SEASON_YEAR).first()
    if existing:
        db.query(AuditLog).filter(
            AuditLog.target_type.in_(["season", "week", "game", "pick"])
        ).filter(
            AuditLog.detail.like("%test%")
        ).delete(synchronize_session=False)
        week_ids = [w.id for w in db.query(Week).filter(Week.season_id == existing.id).all()]
        if week_ids:
            game_ids = [g.id for g in db.query(Game).filter(Game.week_id.in_(week_ids)).all()]
            if game_ids:
                db.query(Pick).filter(Pick.game_id.in_(game_ids)).delete(synchronize_session=False)
            db.query(Game).filter(Game.week_id.in_(week_ids)).delete(synchronize_session=False)
        db.query(Week).filter(Week.season_id == existing.id).delete(synchronize_session=False)
        db.delete(existing)
        db.flush()

    db.query(Season).update({"is_active": False})
    season = Season(year=TEST_SEASON_YEAR, is_active=True)
    db.add(season)
    db.flush()

    now = datetime.utcnow()

    # Week 1 — open, picks unlocked, kickoff tomorrow
    week1 = Week(
        season_id=season.id, week_number=1, label="Week 1 (Test — Open)",
        espn_week=1, first_kickoff=now + timedelta(days=1),
        is_picks_locked=False, is_spreads_locked=True, is_completed=False,
    )
    db.add(week1)
    db.flush()

    for i, (awt, awn, hwt, hwn, spread) in enumerate(_OPEN_GAMES):
        db.add(Game(
            week_id=week1.id,
            espn_game_id=f"test_open_{i}",
            away_team=awt, away_team_name=awn,
            away_team_logo=f"{ESPN_CDN}/{awt.lower()}.png",
            home_team=hwt, home_team_name=hwn,
            home_team_logo=f"{ESPN_CDN}/{hwt.lower()}.png",
            kickoff_time=now + timedelta(days=1, hours=i),
            spread=spread, spread_source=SpreadSource.manual,
        ))

    # Week 2 — completed, final scores, picks locked
    week2 = Week(
        season_id=season.id, week_number=2, label="Week 2 (Test — Completed)",
        espn_week=2, first_kickoff=now - timedelta(days=7),
        is_picks_locked=True, is_spreads_locked=True, is_completed=True,
    )
    db.add(week2)
    db.flush()

    all_users = db.query(User).filter(User.is_active == True).all()
    n = len(_DONE_GAMES)

    for i, (awt, awn, hwt, hwn, spread, ascore, hscore) in enumerate(_DONE_GAMES):
        from app.services.scoring import compute_home_covered
        home_covered = compute_home_covered(hscore, ascore, spread)
        game = Game(
            week_id=week2.id,
            espn_game_id=f"test_done_{i}",
            away_team=awt, away_team_name=awn,
            away_team_logo=f"{ESPN_CDN}/{awt.lower()}.png",
            home_team=hwt, home_team_name=hwn,
            home_team_logo=f"{ESPN_CDN}/{hwt.lower()}.png",
            kickoff_time=now - timedelta(days=7, hours=i),
            spread=spread, spread_source=SpreadSource.manual,
            away_score=ascore, home_score=hscore,
            is_final=True, home_covered=home_covered,
        )
        db.add(game)
        db.flush()

        # Give each user a pick for this game (rotate teams, distribute points)
        for u_idx, u in enumerate(all_users):
            picked = hwt if (i + u_idx) % 2 == 0 else awt
            pts = (i + u_idx) % n + 1
            is_correct = (picked == hwt and home_covered) or (picked == awt and not home_covered)
            pick = Pick(
                user_id=u.id, game_id=game.id,
                week_id=week2.id, season_id=season.id,
                picked_team=picked, confidence_points=pts,
                is_correct=is_correct,
                points_earned=float(pts) if is_correct else 0.0,
            )
            db.add(pick)

    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/test-season/delete")
async def delete_test_season(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    season = db.query(Season).filter(Season.year == TEST_SEASON_YEAR).first()
    if season:
        week_ids = [w.id for w in db.query(Week).filter(Week.season_id == season.id).all()]
        if week_ids:
            game_ids = [g.id for g in db.query(Game).filter(Game.week_id.in_(week_ids)).all()]
            if game_ids:
                db.query(Pick).filter(Pick.game_id.in_(game_ids)).delete(synchronize_session=False)
            db.query(Game).filter(Game.week_id.in_(week_ids)).delete(synchronize_session=False)
        db.query(Week).filter(Week.season_id == season.id).delete(synchronize_session=False)
        db.delete(season)
        db.commit()

    return RedirectResponse(url="/admin/", status_code=303)



# ─── Contributor routes ────────────────────────────────────────────────────────

@router.get("/spreads", response_class=HTMLResponse)
async def spreads_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        return RedirectResponse(url="/", status_code=303)

    season = db.query(Season).filter(Season.is_active == True).first()
    weeks = []
    selected_week = None
    games = []

    if season:
        weeks = (
            db.query(Week)
            .filter(Week.season_id == season.id)
            .order_by(Week.week_number)
            .all()
        )

        # Honor ?week_id= param; otherwise default to first incomplete week
        week_id_param = request.query_params.get("week_id")
        if week_id_param:
            selected_week = next((w for w in weeks if str(w.id) == week_id_param), None)
        if not selected_week:
            selected_week = next((w for w in weeks if not w.is_completed), None)
        if not selected_week and weeks:
            selected_week = weeks[-1]

        if selected_week:
            games = (
                db.query(Game)
                .filter(Game.week_id == selected_week.id)
                .order_by(Game.kickoff_time)
                .all()
            )

    return templates.TemplateResponse(
        "admin/spreads.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "weeks": weeks,
            "selected_week": selected_week,
            "games": games,
        },
    )


@router.post("/spreads/update")
async def update_spread(
    request: Request,
    game_id: int = Form(...),
    spread: float = Form(...),
    redirect_week_id: int = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    week = db.query(Week).filter(Week.id == game.week_id).first()
    if week and week.is_spreads_locked and user.role != Role.admin:
        raise HTTPException(status_code=400, detail="Spreads are locked for this week")

    from app.services.odds import round_spread_down
    old_spread = game.spread
    game.spread = round_spread_down(spread)
    game.spread_source = SpreadSource.manual
    game.spread_override_by = user.id
    game.spread_updated_at = datetime.utcnow()

    log = AuditLog(
        user_id=user.id,
        action="update_spread",
        target_type="game",
        target_id=game_id,
        detail=f"Spread changed from {old_spread} to {game.spread}",
    )
    db.add(log)
    db.commit()
    dest = f"/admin/spreads?week_id={redirect_week_id}" if redirect_week_id else "/admin/spreads"
    return RedirectResponse(url=dest, status_code=303)


def _parse_score(raw: str | None) -> int | None:
    """Read a score box. An empty box means "no score", not a 422.

    The form posts empty strings for blank number inputs, and declaring these
    as ``int | None`` made FastAPI reject the whole save — so half-filling a
    game (or clearing one box) looked to a contributor like the app simply
    refused to save.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


@router.post("/scores/update")
async def update_score(
    request: Request,
    game_id: int = Form(...),
    home_score: str | None = Form(None),
    away_score: str | None = Form(None),
    is_final: bool = Form(False),
    redirect_week_id: int = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    home_score = _parse_score(home_score)
    away_score = _parse_score(away_score)

    old_final = game.is_final
    scores_cleared = home_score is None or away_score is None

    # Cannot be final without scores
    if scores_cleared:
        is_final = False

    # If game was final and is now being un-finaled, reset picks to pending
    if old_final and (not is_final or scores_cleared):
        unscore_game(db, game)

    game.home_score = home_score
    game.away_score = away_score
    game.is_final = is_final
    # A final entered by hand outranks the feed, which must not overwrite it on
    # its next pass — that is what made saved finals disappear minutes later.
    # A score typed in before the game ends is provisional, so the live feed is
    # still welcome to refine it.
    game.score_source = ScoreSource.manual if is_final else ScoreSource.api
    game.score_updated_at = datetime.utcnow()
    if is_final:
        game.is_in_progress = False

    db.add(AuditLog(
        user_id=user.id,
        action="update_score",
        target_type="game",
        target_id=game_id,
        detail=f"Score set to {away_score}@{home_score}, final={is_final}",
    ))

    if is_final:
        # Also covers correcting an already-final score: coverage and every
        # pick on the game are recomputed from the score now on the row.
        update_game_results(db, game)
    else:
        db.commit()

    dest = f"/admin/scores?week_id={redirect_week_id}" if redirect_week_id else "/admin/scores"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/scores/clear")
async def clear_score(
    request: Request,
    game_id: int = Form(...),
    redirect_week_id: int = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    unscore_game(db, game)
    # Cleared by hand means "I have nothing" — let the feed fill it in again.
    game.score_source = ScoreSource.api
    game.score_updated_at = datetime.utcnow()

    db.add(AuditLog(
        user_id=user.id,
        action="clear_score",
        target_type="game",
        target_id=game_id,
        detail=f"Score cleared ({game.away_team}@{game.home_team})",
    ))
    db.commit()

    dest = f"/admin/scores?week_id={redirect_week_id}" if redirect_week_id else "/admin/scores"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/scores/sync")
async def sync_scores_now(
    request: Request,
    week_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Run the score sync for one week on demand, and say what it did.

    Scores had no equivalent of the spreads page's "Sync Odds": they arrived
    only from a background job that reported nothing anywhere a contributor
    could see it, so a sync that matched none of the week's games looked
    exactly like a sync that was never running.
    """
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    from app.services.scheduler import sync_one_week_scores, describe_sync_summary
    from urllib.parse import quote

    status = await sync_one_week_scores(week_id)
    summaries = status.get("weeks") or []
    msg = describe_sync_summary(summaries[0]) if summaries else (
        status.get("error") or "Nothing to sync."
    )
    return RedirectResponse(
        url=f"/admin/scores?week_id={week_id}&msg={quote(msg)}", status_code=303
    )


@router.get("/scores", response_class=HTMLResponse)
async def scores_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        return RedirectResponse(url="/", status_code=303)

    season = db.query(Season).filter(Season.is_active == True).first()
    weeks = []
    selected_week = None
    games = []

    if season:
        weeks = (
            db.query(Week)
            .filter(Week.season_id == season.id)
            .order_by(Week.week_number)
            .all()
        )

        # Honor ?week_id= param; otherwise open on the week being played.
        # Not "the first incomplete week": one week left open by a game that
        # never got a score keeps that page pinned to it for the rest of the
        # season, while the scores someone actually came here to enter sit a
        # click away behind the week selector.
        week_id_param = request.query_params.get("week_id")
        if week_id_param:
            selected_week = next((w for w in weeks if str(w.id) == week_id_param), None)
        if not selected_week:
            now = datetime.utcnow()
            started = [w for w in weeks if w.first_kickoff and w.first_kickoff <= now]
            selected_week = started[-1] if started else None
        if not selected_week:
            selected_week = next((w for w in weeks if not w.is_completed), None)
        if not selected_week and weeks:
            selected_week = weeks[-1]  # fall back to last week of season

        if selected_week:
            games = (
                db.query(Game)
                .filter(Game.week_id == selected_week.id)
                .order_by(Game.kickoff_time)
                .all()
            )

    from app.services.scheduler import get_score_sync_status

    return templates.TemplateResponse(
        "admin/scores.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "weeks": weeks,
            "current_week": selected_week,
            "games": games,
            "sync_status": get_score_sync_status(db),
        },
    )


# ─── Admin-only routes ─────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def admin_home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    seasons = db.query(Season).order_by(Season.year.desc()).all()
    season_weeks = {}
    for season in seasons:
        season_weeks[season.id] = (
            db.query(Week)
            .filter(Week.season_id == season.id)
            .order_by(Week.week_number)
            .all()
        )
    users = db.query(User).order_by(User.last_name, User.first_name).all()
    recent_logs = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(20)
        .all()
    )
    # How many push subscriptions each user has (for the notification sender)
    sub_counts = {}
    for row in db.query(PushSubscription.user_id).all():
        sub_counts[row.user_id] = sub_counts.get(row.user_id, 0) + 1

    # Issue reporting ("Submit an Issue" -> GitHub). The token itself never
    # reaches the page — only a masked hint that one is saved.
    gh = github_issues.get_config(db)
    github_settings = {
        "repo": gh["repo"],
        "repo_source": gh["repo_source"],
        "token_source": gh["token_source"],
        "token_hint": github_issues.mask_token(gh["token"]),
        "configured": gh["configured"],
    }

    current_year = datetime.utcnow().year
    return templates.TemplateResponse(
        "admin/home.html",
        {
            "request": request,
            "user": user,
            "github_settings": github_settings,
            "seasons": seasons,
            "season_weeks": season_weeks,
            "users": users,
            "invites": _load_invites(db),
            "new_invite": request.query_params.get("new_invite"),
            "roles": Role,
            "sub_counts": sub_counts,
            "recent_logs": recent_logs,
            "current_year": current_year,
            "historical_sync": request.query_params.get("historical_sync"),
            "msg": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/season/create")
async def create_season(
    request: Request,
    background_tasks: BackgroundTasks,
    year: int = Form(...),
    make_active: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    if db.query(Season).filter(Season.year == year).first():
        raise HTTPException(status_code=400, detail=f"Season {year} already exists")

    if make_active:
        db.query(Season).update({"is_active": False})

    season = Season(year=year, is_active=make_active)
    db.add(season)
    db.commit()
    db.refresh(season)

    # Auto-create all 18 regular season weeks
    for n in range(1, 19):
        week = Week(
            season_id=season.id,
            week_number=n,
            label=f"Week {n}",
            espn_week=n,
        )
        db.add(week)

    is_historical = year < datetime.utcnow().year and year != 9999
    detail = f"Created season {year} with 18 weeks"
    if is_historical:
        detail += " (historical sync queued)"

    log = AuditLog(user_id=user.id, action="create_season", target_type="season",
                   target_id=season.id, detail=detail)
    db.add(log)
    db.commit()

    if is_historical:
        from app.services.scheduler import sync_historical_season
        background_tasks.add_task(sync_historical_season, season.id, year)
        return RedirectResponse(url="/admin/?historical_sync=1", status_code=303)

    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/season/{season_id}/sync-all")
async def sync_all_weeks(
    request: Request,
    season_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    season = db.query(Season).filter(Season.id == season_id).first()
    if not season:
        raise HTTPException(status_code=404)

    from app.services.scheduler import sync_historical_season
    background_tasks.add_task(sync_historical_season, season.id, season.year)
    return RedirectResponse(url="/admin/?historical_sync=1", status_code=303)


@router.post("/season/{season_id}/activate")
async def activate_season(request: Request, season_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    season = db.query(Season).filter(Season.id == season_id).first()
    if not season:
        raise HTTPException(status_code=404)

    db.query(Season).update({"is_active": False})
    season.is_active = True
    log = AuditLog(user_id=user.id, action="activate_season", target_type="season",
                   target_id=season.id, detail=f"Activated season {season.year}")
    db.add(log)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/season/{season_id}/deactivate")
async def deactivate_season(request: Request, season_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    season = db.query(Season).filter(Season.id == season_id).first()
    if not season:
        raise HTTPException(status_code=404)

    season.is_active = False
    log = AuditLog(user_id=user.id, action="deactivate_season", target_type="season",
                   target_id=season.id, detail=f"Deactivated season {season.year}")
    db.add(log)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.get("/week/{week_id}", response_class=HTMLResponse)
async def week_admin(request: Request, week_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        raise HTTPException(status_code=404)

    games = (
        db.query(Game)
        .filter(Game.week_id == week_id)
        .order_by(Game.kickoff_time)
        .all()
    )
    all_users = db.query(User).filter(User.is_active == True).all()

    return templates.TemplateResponse(
        "admin/week.html",
        {
            "request": request,
            "user": user,
            "week": week,
            "games": games,
            "all_users": all_users,
            "sync_ok": request.query_params.get("sync_ok"),
            "sync_error": request.query_params.get("sync_error"),
        },
    )


@router.post("/week/{week_id}/sync")
async def sync_week(request: Request, week_id: int, db: Session = Depends(get_db)):
    """Manually trigger ESPN schedule sync for a week."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        raise HTTPException(status_code=404)

    season = db.query(Season).filter(Season.id == week.season_id).first()
    from app.services.scheduler import sync_week_schedule
    from urllib.parse import quote
    count, error = await sync_week_schedule(season.year, week.week_number, week.espn_week or week.week_number)

    if error:
        return RedirectResponse(url=f"/admin/week/{week_id}?sync_error={quote(error)}", status_code=303)
    return RedirectResponse(url=f"/admin/week/{week_id}?sync_ok={count}", status_code=303)


@router.post("/week/{week_id}/sync-odds")
async def sync_week_odds(
    request: Request,
    week_id: int,
    redirect_to: str = Form(None),
    db: Session = Depends(get_db),
):
    """Manually pull spreads from The Odds API for this week (contributor+)."""
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        raise HTTPException(status_code=404)

    from urllib.parse import quote

    # Return to whichever page triggered the sync.
    dest = f"/admin/spreads?week_id={week_id}" if redirect_to == "spreads" else f"/admin/week/{week_id}"
    sep = "&" if "?" in dest else "?"

    if week.is_spreads_locked and user.role != Role.admin:
        return RedirectResponse(
            url=f"{dest}{sep}error={quote('Spreads are locked for this week')}",
            status_code=303,
        )

    from app.services.scheduler import sync_week_spreads
    count, error = await sync_week_spreads(week_id)

    if error:
        return RedirectResponse(url=f"{dest}{sep}error={quote(error)}", status_code=303)

    db.add(AuditLog(
        user_id=user.id,
        action="sync_odds",
        target_type="week",
        target_id=week_id,
        detail=f"Synced odds for {count} game(s) in week {week.week_number}",
    ))
    db.commit()
    msg = f"Updated odds for {count} game(s)." if count else "No odds updated (no matching lines, or all spreads are manual)."
    return RedirectResponse(url=f"{dest}{sep}msg={quote(msg)}", status_code=303)


@router.post("/week/{week_id}/game/{game_id}/kickoff")
async def update_kickoff(
    request: Request,
    week_id: int,
    game_id: int,
    kickoff: str = Form(...),
    db: Session = Depends(get_db),
):
    """Admin edits a game's kickoff date/time. Input is Eastern; stored as UTC."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id, Game.week_id == week_id).first()
    if not game:
        raise HTTPException(status_code=404)

    from urllib.parse import quote
    try:
        # datetime-local sends "YYYY-MM-DDTHH:MM" (or with seconds) in ET wall-clock
        et_naive = datetime.fromisoformat(kickoff)
    except (ValueError, TypeError):
        return RedirectResponse(
            url=f"/admin/week/{week_id}?sync_error={quote('Invalid kickoff date/time')}",
            status_code=303,
        )

    old_utc = game.kickoff_time
    game.kickoff_time = eastern_to_utc(et_naive.replace(tzinfo=None))

    # Keep the week's derived lock times in sync with the earliest kickoff.
    week = db.query(Week).filter(Week.id == week_id).first()
    kickoffs = [
        g.kickoff_time
        for g in db.query(Game).filter(Game.week_id == week_id).all()
        if g.kickoff_time is not None
    ]
    if week and kickoffs:
        week.first_kickoff = min(kickoffs)
        week.spread_lock_time = week.first_kickoff - timedelta(hours=24)

    old_et = to_eastern(old_utc).strftime("%Y-%m-%d %H:%M ET") if old_utc else "TBD"
    new_et = to_eastern(game.kickoff_time).strftime("%Y-%m-%d %H:%M ET")
    db.add(AuditLog(
        user_id=user.id,
        action="edit_kickoff",
        target_type="game",
        target_id=game_id,
        detail=f"{game.away_team} @ {game.home_team} kickoff changed from {old_et} to {new_et}",
    ))
    db.commit()
    return RedirectResponse(
        url=f"/admin/week/{week_id}?msg={quote('Kickoff updated for ' + game.away_team + ' @ ' + game.home_team)}",
        status_code=303,
    )


@router.post("/week/{week_id}/lock-spreads")
async def lock_spreads(request: Request, week_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    week = db.query(Week).filter(Week.id == week_id).first()
    week.is_spreads_locked = True
    db.commit()
    return RedirectResponse(url=f"/admin/week/{week_id}", status_code=303)


@router.post("/week/{week_id}/lock-picks")
async def lock_picks(request: Request, week_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    week = db.query(Week).filter(Week.id == week_id).first()
    week.is_picks_locked = True
    week.picks_lock_override = False  # re-enable auto-lock behaviour
    db.commit()
    return RedirectResponse(url=f"/admin/week/{week_id}", status_code=303)


@router.post("/week/{week_id}/unlock-picks")
async def unlock_picks(request: Request, week_id: int, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    week = db.query(Week).filter(Week.id == week_id).first()
    week.is_picks_locked = False
    week.picks_lock_override = True  # prevent scheduler from re-locking
    log = AuditLog(user_id=user.id, action="unlock_picks", target_type="week",
                   target_id=week_id, detail=f"Picks unlocked for week {week.week_number}")
    db.add(log)
    db.commit()
    return RedirectResponse(url=f"/admin/week/{week_id}", status_code=303)


@router.get("/picks/edit/{user_id}/{week_id}", response_class=HTMLResponse)
async def edit_user_picks(
    request: Request,
    user_id: int,
    week_id: int,
    db: Session = Depends(get_db),
):
    """Admin can edit any user's picks."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    target_user = db.query(User).filter(User.id == user_id).first()
    week = db.query(Week).filter(Week.id == week_id).first()
    if not target_user or not week:
        raise HTTPException(status_code=404)

    from app.routers.picks import build_pick_context
    ctx = build_pick_context(db, week, user, admin_user_id=user_id)
    ctx.update({
        "request": request,
        "user": user,
        "target_user": target_user,
        "is_admin_edit": True,
    })
    return templates.TemplateResponse("admin/edit_picks.html", ctx)


@router.post("/picks/edit/{user_id}/{week_id}")
async def save_user_picks(
    request: Request,
    user_id: int,
    week_id: int,
    db: Session = Depends(get_db),
):
    """Admin saves edited picks for a user."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    target_user = db.query(User).filter(User.id == user_id).first()
    week = db.query(Week).filter(Week.id == week_id).first()
    if not target_user or not week:
        raise HTTPException(status_code=404)

    form = await request.form()
    games = db.query(Game).filter(Game.week_id == week_id).all()
    n_games = len(games)
    max_full = 16
    available_points = set(range(max_full - n_games + 1, max_full + 1))

    new_picks = {}
    for game in games:
        picked_team = form.get(f"game_{game.id}_team")
        points_str = form.get(f"game_{game.id}_points")
        if not picked_team or not points_str:
            continue
        points = int(points_str)
        new_picks[game.id] = (points, picked_team)

    from app.routers.picks import apply_picks

    previous = apply_picks(db, user_id, week, new_picks)

    for game_id, (points, team) in new_picks.items():
        if game_id in previous:
            old_points, old_team = previous[game_id]
            detail = f"Changed from {old_team}/{old_points} to {team}/{points}"
        else:
            detail = f"Admin created pick: {team}/{points}"

        log = AuditLog(
            user_id=user.id,
            action="edit_pick",
            target_type="pick",
            target_id=game_id,
            detail=detail + f" for user {target_user.full_name}",
        )
        db.add(log)

    db.commit()
    return RedirectResponse(url=f"/admin/week/{week_id}", status_code=303)


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    # Users management is now inline on the admin home page
    msg = request.query_params.get("msg", "")
    error = request.query_params.get("error", "")
    qs = ""
    if msg:
        qs = f"?msg={msg}"
    elif error:
        qs = f"?error={error}"
    return RedirectResponse(url=f"/admin/{qs}", status_code=303)


@router.post("/users/add")
async def add_user(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse(url="/admin/?error=Email+already+registered", status_code=303)

    new_user = User(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=email,
        password_hash=hash_password(password),
        role=Role.player,
        is_active=True,
    )
    db.add(new_user)
    db.flush()
    db.add(AuditLog(
        user_id=user.id,
        action="add_user",
        target_type="user",
        target_id=new_user.id,
        detail=f"Created player account for {new_user.full_name} ({email})",
    ))
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/users/{user_id}/role")
async def update_user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    old_role = target.role
    target.role = Role(role)
    log = AuditLog(
        user_id=user.id,
        action="change_role",
        target_type="user",
        target_id=user_id,
        detail=f"Role changed from {old_role} to {role}",
    )
    db.add(log)
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/users/{user_id}/toggle")
async def toggle_user(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    target = db.query(User).filter(User.id == user_id).first()
    if not target or target.id == user.id:
        raise HTTPException(status_code=400)

    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse(url="/admin/", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
):
    """Permanently delete a user and all of their dependent records."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)
    if target.id == user.id:
        return RedirectResponse(
            url="/admin/?error=" + quote("You can't delete your own account"),
            status_code=303,
        )
    # Never remove the last remaining admin.
    if target.role == Role.admin:
        admin_count = db.query(User).filter(User.role == Role.admin).count()
        if admin_count <= 1:
            return RedirectResponse(
                url="/admin/?error=" + quote("Can't delete the only admin account"),
                status_code=303,
            )

    target_name = target.full_name
    target_email = target.email

    # Remove dependent rows first (SQLite doesn't enforce FKs by default, but we
    # clean up explicitly so no orphaned picks/subscriptions/transactions remain).
    db.query(Pick).filter(Pick.user_id == user_id).delete(synchronize_session=False)
    db.query(PushSubscription).filter(
        PushSubscription.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(Transaction).filter(
        (Transaction.user_id == user_id) | (Transaction.logged_by_id == user_id)
    ).delete(synchronize_session=False)
    # Audit logs authored by the deleted user would dangle their FK; drop them.
    db.query(AuditLog).filter(AuditLog.user_id == user_id).delete(synchronize_session=False)

    db.delete(target)
    db.flush()

    db.add(AuditLog(
        user_id=user.id,
        action="delete_user",
        target_type="user",
        target_id=user_id,
        detail=f"Permanently deleted {target_name} ({target_email})",
    ))
    db.commit()
    return RedirectResponse(
        url="/admin/?msg=" + quote(f"Deleted {target_name}"),
        status_code=303,
    )


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)
    if len(new_password) < 8:
        return RedirectResponse(url="/admin/?error=Password+must+be+at+least+8+characters", status_code=303)

    target.password_hash = hash_password(new_password)
    db.add(AuditLog(
        user_id=user.id,
        action="reset_password",
        target_type="user",
        target_id=user_id,
        detail=f"Password reset for {target.full_name}",
    ))
    db.commit()
    return RedirectResponse(url="/admin/?msg=Password+reset+successfully", status_code=303)


# ── Invites ──────────────────────────────────────────────────────────────────
# Registration is invite-only, so these are the only way (short of adding a
# player directly above) for someone new to get an account.

def _load_invites(db: Session):
    """Newest first, with still-usable invites pinned to the top."""
    invites = (
        db.query(Invite)
        .order_by(Invite.created_at.desc(), Invite.id.desc())
        .all()
    )
    return sorted(invites, key=lambda i: 0 if i.is_valid else 1)


@router.post("/invites/create")
async def create_invite(
    request: Request,
    email: str = Form(""),
    note: str = Form(""),
    expires_days: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    email = (email or "").strip().lower()
    if email and db.query(User).filter(User.email == email).first():
        return RedirectResponse(
            url="/admin/?error=" + quote(f"{email} already has an account"),
            status_code=303,
        )

    expires_at = None
    if expires_days:
        try:
            days = int(expires_days)
        except ValueError:
            days = 0
        if days > 0:
            expires_at = datetime.utcnow() + timedelta(days=days)

    # Codes are random, but a collision would violate the unique index — retry
    # a few times rather than 500 on astronomically bad luck.
    for _ in range(10):
        code = generate_invite_code()
        if not db.query(Invite).filter(Invite.code == code).first():
            break
    else:
        return RedirectResponse(
            url="/admin/?error=" + quote("Could not generate an invite code, try again"),
            status_code=303,
        )

    invite = Invite(
        code=code,
        email=email or None,
        note=(note or "").strip() or None,
        created_by_id=user.id,
        expires_at=expires_at,
    )
    db.add(invite)
    db.flush()
    db.add(AuditLog(
        user_id=user.id,
        action="create_invite",
        target_type="invite",
        target_id=invite.id,
        detail=f"Created invite {code}" + (f" for {email}" if email else ""),
    ))
    db.commit()
    return RedirectResponse(url=f"/admin/?new_invite={code}#invites", status_code=303)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    request: Request,
    invite_id: int,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote
    invite = db.query(Invite).filter(Invite.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404)
    if invite.is_used:
        return RedirectResponse(
            url="/admin/?error=" + quote("That invite has already been used"),
            status_code=303,
        )

    invite.revoked_at = datetime.utcnow()
    db.add(AuditLog(
        user_id=user.id,
        action="revoke_invite",
        target_type="invite",
        target_id=invite.id,
        detail=f"Revoked invite {invite.code}",
    ))
    db.commit()
    return RedirectResponse(
        url="/admin/?msg=" + quote("Invite revoked") + "#invites",
        status_code=303,
    )


@router.post("/invites/{invite_id}/delete")
async def delete_invite(
    request: Request,
    invite_id: int,
    db: Session = Depends(get_db),
):
    """Remove an invite row outright — housekeeping for the list."""
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote
    invite = db.query(Invite).filter(Invite.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404)

    code = invite.code
    db.delete(invite)
    db.add(AuditLog(
        user_id=user.id,
        action="delete_invite",
        target_type="invite",
        target_id=invite_id,
        detail=f"Deleted invite {code}",
    ))
    db.commit()
    return RedirectResponse(
        url="/admin/?msg=" + quote("Invite deleted") + "#invites",
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Prize payouts — the plan, the playground, and what it all pays out
# ---------------------------------------------------------------------------

def _num(value, default: float = 0.0) -> float:
    """A form field that may be blank, absent, or nonsense."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _plan_from_form(form) -> payouts.Plan:
    """Build a plan out of a submitted playground form.

    Places and awards left blank or set to zero are simply not prizes, so they
    drop out here rather than being stored as $0 rules.
    """
    weekly: dict[int, float] = {}
    season: dict[int, float] = {}
    for prefix, target in ((payouts.WEEKLY, weekly), (payouts.SEASON, season)):
        places = form.getlist(f"{prefix}_place")
        amounts = form.getlist(f"{prefix}_amount")
        for place, amount in zip(places, amounts):
            try:
                rank = int(str(place).strip())
            except (TypeError, ValueError):
                continue
            value = _num(amount)
            if rank > 0 and value > 0:
                target[rank] = round(value, 2)

    awards: dict[str, float] = {}
    for award_id, amount in zip(form.getlist("award_id"), form.getlist("award_amount")):
        value = _num(amount)
        if award_id and value > 0:
            awards[award_id] = round(value, 2)

    paid_weeks = int(_num(form.get("paid_weeks"), payouts.DEFAULT_PAID_WEEKS))
    return payouts.Plan(
        pool=round(max(0.0, _num(form.get("pool_amount"))), 2),
        paid_weeks=max(0, min(paid_weeks, 30)),
        weekly=weekly,
        season=season,
        awards=awards,
        notes=(form.get("notes") or "").strip(),
        is_configured=True,
    )


def _place_rows(amounts: dict[int, float], minimum: int) -> list[int]:
    """The place numbers the editor renders — always a few spare rows."""
    highest = max(list(amounts.keys()) + [0])
    return list(range(1, max(minimum, highest) + 1))


def _render_payouts(
    request: Request,
    db: Session,
    user: User,
    season: Season,
    plan: payouts.Plan,
    *,
    is_preview: bool = False,
    msg: str | None = None,
    error: str | None = None,
):
    """Render the payouts page for a plan — saved or straight off the form."""
    saved_plan = payouts.load_plan(db, season.id)
    report = payouts.compute_payouts(db, season, plan)
    active_users = (
        db.query(User)
        .filter(User.is_active == True)  # noqa: E712
        .order_by(User.last_name, User.first_name)
        .all()
    )
    return templates.TemplateResponse("admin/payouts.html", {
        "request":        request,
        "user":           user,
        "season":         season,
        "seasons":        db.query(Season).order_by(Season.year.desc()).all(),
        "plan":           plan,
        "totals":         payouts.plan_totals(plan),
        "report":         report,
        "ledger":         payouts.payout_ledger(db, report, active_users),
        "weekly_places":  _place_rows(plan.weekly, payouts.MIN_WEEKLY_PLACES),
        "season_places":  _place_rows(plan.season, payouts.MIN_SEASON_PLACES),
        "awards":         [a for a in AWARD_REGISTRY if a.enabled],
        "is_preview":     is_preview,
        "has_saved_plan": saved_plan.is_configured,
        "season_weeks":   payouts.default_paid_weeks(db, season.id),
        "msg":            msg or request.query_params.get("msg"),
        "error":          error or request.query_params.get("error"),
    })


def _payouts_season(db: Session, season_id: int | None) -> Season | None:
    if season_id:
        return db.query(Season).filter(Season.id == season_id).first()
    return db.query(Season).filter(Season.is_active == True).first()  # noqa: E712


@router.get("/payouts", response_class=HTMLResponse)
async def payouts_page(
    request: Request,
    season_id: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    season = _payouts_season(db, season_id)
    if not season:
        return templates.TemplateResponse(
            "dashboard/no_season.html", {"request": request, "user": user}
        )

    return _render_payouts(request, db, user, season, payouts.load_plan(db, season.id))


@router.post("/payouts/preview", response_class=HTMLResponse)
async def preview_payouts(request: Request, db: Session = Depends(get_db)):
    """Price a set of numbers without committing to them.

    The whole point of the playground: change first place to $15 and see what
    the season would have paid so far, then decide whether to save it.
    """
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    form = await request.form()
    season = _payouts_season(db, int(_num(form.get("season_id"))) or None)
    if not season:
        return RedirectResponse(url="/admin/payouts", status_code=303)

    return _render_payouts(
        request, db, user, season, _plan_from_form(form),
        is_preview=True,
        msg="Previewing these numbers — nothing is saved until you hit Save Plan.",
    )


@router.post("/payouts/save")
async def save_payouts(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    form = await request.form()
    season = _payouts_season(db, int(_num(form.get("season_id"))) or None)
    if not season:
        return RedirectResponse(url="/admin/payouts", status_code=303)

    from urllib.parse import quote

    plan = _plan_from_form(form)
    payouts.save_plan(db, season, plan, user)

    totals = payouts.plan_totals(plan)
    if totals["is_balanced"]:
        msg = f"Plan saved — the full ${totals['pool']:.2f} pool is allocated."
    elif totals["is_over"]:
        msg = (
            f"Plan saved, but it pays out ${abs(totals['remaining']):.2f} "
            f"more than the ${totals['pool']:.2f} pool."
        )
    else:
        msg = (
            f"Plan saved — ${totals['remaining']:.2f} of the "
            f"${totals['pool']:.2f} pool is still unallocated."
        )
    return RedirectResponse(
        url=f"/admin/payouts?season_id={season.id}&msg={quote(msg)}",
        status_code=303,
    )


def _get_fund_settings(db: Session) -> dict:
    rows = {r.key: r.value for r in db.query(AppSetting).filter(
        AppSetting.key.in_(["entry_fee", "payment_venmo", "payment_paypal", "payment_cashapp", "payment_zelle"])
    ).all()}
    return {
        "entry_fee": float(rows.get("entry_fee") or 0),
        "venmo":     rows.get("payment_venmo", ""),
        "paypal":    rows.get("payment_paypal", ""),
        "cashapp":   rows.get("payment_cashapp", ""),
        "zelle":     rows.get("payment_zelle", ""),
    }


@router.get("/funds", response_class=HTMLResponse)
async def funds_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    settings = _get_fund_settings(db)
    entry_fee = settings["entry_fee"]

    users = db.query(User).filter(User.is_active == True).order_by(User.last_name, User.first_name).all()

    user_stats = []
    for u in users:
        txns = db.query(Transaction).filter(Transaction.user_id == u.id).all()
        paid_in  = sum(t.amount for t in txns if t.direction == "in")
        received = sum(t.amount for t in txns if t.direction == "out")
        user_stats.append({
            "user":     u,
            "paid_in":  paid_in,
            "received": received,
            "balance":  entry_fee - paid_in,
        })

    all_transactions = (
        db.query(Transaction)
        .order_by(Transaction.created_at.desc())
        .all()
    )

    # What the prize plan says the league owes, against what has been logged.
    # Recomputed on every load rather than stored, so editing the plan moves
    # these numbers for weeks that have already been played.
    season = db.query(Season).filter(Season.is_active == True).first()  # noqa: E712
    plan = payouts.load_plan(db, season.id) if season else payouts.Plan()
    report = payouts.compute_payouts(db, season, plan) if season else payouts.PayoutReport(plan)
    # Somebody who has left the league can still be owed for a week they won,
    # so the ledger covers anyone with money on either side of it.
    ledger_users = list(users)
    known = {u.id for u in ledger_users}
    for line in report.lines:
        if line.user.id not in known:
            ledger_users.append(line.user)
            known.add(line.user.id)
    ledger = payouts.payout_ledger(db, report, ledger_users)
    total_owed = round(sum(row["owed"] for row in ledger if row["owed"] > 0), 2)

    return templates.TemplateResponse("admin/funds.html", {
        "request":           request,
        "user":              user,
        "settings":          settings,
        "entry_fee":         entry_fee,
        "user_stats":        user_stats,
        "all_transactions":  all_transactions,
        "total_pool":        entry_fee * len(users),
        "total_collected":   sum(s["paid_in"]  for s in user_stats),
        "total_outstanding": sum(max(0, s["balance"]) for s in user_stats),
        "total_paid_out":    sum(s["received"] for s in user_stats),
        "users":             users,
        "season":            season,
        "plan":              plan,
        "plan_totals":       payouts.plan_totals(plan),
        "report":            report,
        "ledger":            ledger,
        "total_owed":        total_owed,
        # Most recent first: the week you are about to pay out is the one at
        # the top of the page, not the bottom.
        "payout_weeks":      list(reversed(report.weeks)),
        "msg":   request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    })


@router.post("/funds/settings")
async def update_fund_settings(
    request: Request,
    entry_fee: float = Form(0.0),
    payment_venmo:   str = Form(""),
    payment_paypal:  str = Form(""),
    payment_cashapp: str = Form(""),
    payment_zelle:   str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    db.merge(AppSetting(key="entry_fee",        value=str(entry_fee)))
    db.merge(AppSetting(key="payment_venmo",    value=payment_venmo.strip()))
    db.merge(AppSetting(key="payment_paypal",   value=payment_paypal.strip()))
    db.merge(AppSetting(key="payment_cashapp",  value=payment_cashapp.strip()))
    db.merge(AppSetting(key="payment_zelle",    value=payment_zelle.strip()))
    db.add(AuditLog(
        user_id=user.id, action="update_fund_settings",
        detail=f"Entry fee set to ${entry_fee:.2f}",
    ))
    db.commit()
    return RedirectResponse(url="/admin/funds?msg=Settings+saved", status_code=303)


@router.post("/funds/transaction")
async def log_transaction(
    request: Request,
    user_id:   int   = Form(...),
    amount:    float = Form(...),
    direction: str   = Form(...),
    note:      str   = Form(""),
    db: Session = Depends(get_db),
):
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)
    if direction not in ("in", "out"):
        raise HTTPException(status_code=400, detail="Invalid direction")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404)

    db.add(Transaction(
        user_id=user_id, amount=amount, direction=direction,
        note=note.strip() or None, logged_by_id=admin.id,
    ))
    verb = "received from" if direction == "in" else "paid to"
    db.add(AuditLog(
        user_id=admin.id, action="log_transaction",
        target_type="user", target_id=user_id,
        detail=f"${amount:.2f} {verb} {target.full_name}" + (f" — {note}" if note.strip() else ""),
    ))
    db.commit()
    return RedirectResponse(url="/admin/funds", status_code=303)


@router.post("/funds/payouts/settle")
async def settle_payouts(request: Request, db: Session = Depends(get_db)):
    """Log an outgoing transaction for everybody the prize plan still owes.

    Three winners a week is three trips through the same form, so this does the
    round in one go. It only writes down what was paid — the money still has to
    leave by Venmo or a handshake.
    """
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    season = db.query(Season).filter(Season.is_active == True).first()  # noqa: E712
    if not season:
        return RedirectResponse(
            url="/admin/funds?error=No+active+season", status_code=303
        )

    plan = payouts.load_plan(db, season.id)
    report = payouts.compute_payouts(db, season, plan)
    users = {u.id: u for u in db.query(User).all()}
    ledger = payouts.payout_ledger(db, report, list(users.values()))

    note = f"Prize payout ({season.year})"
    paid_count = 0
    paid_total = 0.0
    for row in ledger:
        if row["owed"] <= 0:
            continue
        db.add(Transaction(
            user_id=row["user"].id, amount=row["owed"], direction="out",
            note=note, logged_by_id=admin.id,
        ))
        paid_count += 1
        paid_total += row["owed"]

    if not paid_count:
        return RedirectResponse(
            url="/admin/funds?msg=Nothing+outstanding+to+log", status_code=303
        )

    db.add(AuditLog(
        user_id=admin.id, action="settle_payouts",
        target_type="season", target_id=season.id,
        detail=f"Logged {paid_count} payout(s) totalling ${paid_total:.2f} for {season.year}",
    ))
    db.commit()
    msg = f"Logged {paid_count} payout(s) totalling ${paid_total:.2f}."
    return RedirectResponse(url=f"/admin/funds?msg={quote(msg)}", status_code=303)


@router.post("/funds/transaction/{txn_id}/delete")
async def delete_transaction(
    request: Request,
    txn_id: int,
    db: Session = Depends(get_db),
):
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    txn = db.query(Transaction).filter(Transaction.id == txn_id).first()
    if not txn:
        raise HTTPException(status_code=404)

    db.add(AuditLog(
        user_id=admin.id, action="delete_transaction",
        target_type="user", target_id=txn.user_id,
        detail=f"Deleted ${txn.amount:.2f} {'in' if txn.direction == 'in' else 'out'} for user {txn.user_id}",
    ))
    db.delete(txn)
    db.commit()
    return RedirectResponse(url="/admin/funds", status_code=303)


# ---------------------------------------------------------------------------
# Updating the app from inside the app
# ---------------------------------------------------------------------------

UPDATE_SETTING_KEYS = (
    "update_registry_user", "update_registry_token", "update_container_name",
)


def _update_settings(db: Session) -> dict:
    rows = {
        r.key: (r.value or "").strip()
        for r in db.query(AppSetting).filter(AppSetting.key.in_(UPDATE_SETTING_KEYS)).all()
    }
    return {
        "registry_user":  rows.get("update_registry_user", ""),
        "registry_token": rows.get("update_registry_token", ""),
        "container_name": rows.get("update_container_name", "")
                          or os.getenv("UPDATE_CONTAINER_NAME", ""),
    }


def _update_context(request: Request, db: Session, user: User) -> dict:
    settings = _update_settings(db)
    github = github_issues.get_config(db)
    socket = docker_api.socket_status()
    versions = registry.list_versions(
        github_repo=github["repo"],
        registry_token=settings["registry_token"],
        registry_user=settings["registry_user"],
        # Reusing the issue-reporting token, which is for this same repository,
        # so a branch build can be listed by the pull request's title rather
        # than as a bare tag. Only used to read pull requests, and only to
        # label the list — without it everything still works, less readably.
        github_token=github["token"],
    )
    status = selfupdate.read_status()
    if selfupdate.is_stalled(status):
        status = dict(status, state="failed", message=(
            "The update stopped reporting. Check the container is running, "
            "then try again."
        ))
    return {
        "request":        request,
        "user":           user,
        "socket":         socket,
        "versions":       versions,
        "status":         status,
        "in_progress":    selfupdate.is_running(),
        "settings":       settings,
        # The token itself is never rendered back — only whether one is saved.
        "has_registry_token": bool(settings["registry_token"]),
        "current": {
            "version": main_version(),
            "built_at": os.getenv("BUILD_DATE", ""),
            "commit":  os.getenv("GIT_COMMIT", ""),
            "image":   selfupdate.IMAGE_REPOSITORY,
        },
        "socket_path":    docker_api.default_socket(),
        # Where the "publish a branch build" instructions point.
        "github_repo":    github["repo"],
        "msg":   request.query_params.get("msg"),
        "error": request.query_params.get("error"),
    }


def main_version() -> str:
    return os.getenv("APP_VERSION", "0.0.0-dev")


@router.get("/update", response_class=HTMLResponse)
async def update_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "admin/update.html", _update_context(request, db, user)
    )


@router.get("/update/status")
async def update_status(request: Request, db: Session = Depends(get_db)):
    """Polled while an update runs, including by the version that replaces us.

    The status lives in a file on the data volume precisely so this keeps
    answering across the swap: the page that asked for the update is finished
    by a process that did not exist when it started.
    """
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)
    status = selfupdate.read_status()
    status["running"] = selfupdate.is_running()
    status["current_version"] = main_version()
    if selfupdate.is_stalled(status):
        # Say so rather than spinning: the helper died, or the host rebooted
        # mid-swap. Whatever happened, the answer is to look at the container
        # and try again, not to keep waiting.
        status["stalled"] = True
        status["state"] = "failed"
        status["message"] = (
            "The update stopped reporting. Check the container is running, "
            "then try again."
        )
    return JSONResponse(status)


@router.post("/update/start")
async def start_update(
    request: Request,
    background: BackgroundTasks,
    tag: str = Form(...),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    def back(**params):
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return RedirectResponse(url=f"/admin/update?{query}", status_code=303)

    socket = docker_api.socket_status()
    if not socket["ok"]:
        return back(error=socket["detail"])
    if selfupdate.is_running():
        return back(error="An update is already running.")
    try:
        tag = selfupdate.validate_tag(tag)
    except ValueError as exc:
        return back(error=str(exc))
    if confirm != tag:
        return back(error="The confirmation did not match the version you picked.")

    settings = _update_settings(db)
    db.add(AuditLog(
        user_id=user.id, action="start_update", target_type="app",
        detail=f"Update to {selfupdate.image_ref(tag)} started from {main_version()}",
    ))
    db.commit()

    # After the response: pulling an image is minutes of work, and the browser
    # is going to watch /update/status for the result anyway.
    background.add_task(
        selfupdate.start_update,
        tag,
        started_by=user.full_name,
        registry_user=settings["registry_user"],
        registry_token=settings["registry_token"],
        self_container=settings["container_name"] or None,
    )
    selfupdate.write_status(
        state="pulling", percent=0, tag=tag,
        target_image=selfupdate.image_ref(tag),
        from_version=main_version(), started_by=user.full_name,
        started_at=datetime.utcnow().isoformat(timespec="seconds"),
        message="Starting…", error="",
    )
    return back(msg=f"Updating to {tag}. This page will follow along.")


@router.post("/update/settings")
async def save_update_settings(
    request: Request,
    registry_user: str = Form(""),
    registry_token: str = Form(""),
    container_name: str = Form(""),
    clear_token: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)

    db.merge(AppSetting(key="update_registry_user", value=registry_user.strip()))
    db.merge(AppSetting(key="update_container_name", value=container_name.strip()))
    # A blank box leaves the saved token alone — it is never rendered back, so
    # blank means "unchanged", not "delete it". Clearing is its own button.
    if clear_token:
        db.merge(AppSetting(key="update_registry_token", value=""))
    elif registry_token.strip():
        db.merge(AppSetting(key="update_registry_token", value=registry_token.strip()))

    db.add(AuditLog(
        user_id=user.id, action="update_settings", target_type="app",
        detail="Updater settings saved"
              + (" (registry token cleared)" if clear_token else ""),
    ))
    db.commit()
    registry.clear_cache()
    return RedirectResponse(url="/admin/update?msg=Settings+saved", status_code=303)


@router.post("/update/refresh")
async def refresh_versions(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)
    registry.clear_cache()
    return RedirectResponse(url="/admin/update?msg=Version+list+refreshed", status_code=303)


@router.post("/update/dismiss")
async def dismiss_update_status(request: Request, db: Session = Depends(get_db)):
    """Clear a finished update's banner.

    Only a finished one: while an update is in flight this is the only record
    of what is happening to the container.
    """
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        raise HTTPException(status_code=403)
    if not selfupdate.is_running():
        selfupdate.write_status(
            state="idle", message="No update has been run yet.", error="",
        )
    return RedirectResponse(url="/admin/update", status_code=303)


@router.post("/notify")
async def send_notification(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
    url: str = Form("/"),
    target: str = Form("all"),   # "all" or "select"
    db: Session = Depends(get_db),
):
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    from app.services.notifications import send_to_all, send_to_user
    from urllib.parse import quote

    if target == "all":
        sent = send_to_all(title=title, body=body, url=url)
    else:
        form = await request.form()
        user_ids = [int(v) for v in form.getlist("user_ids") if v.isdigit()]
        sent = 0
        for uid in user_ids:
            u = db.query(User).filter(User.id == uid).first()
            if u:
                sent += send_to_user(user=u, title=title, body=body, url=url, db=db)

    db.add(AuditLog(
        user_id=admin.id,
        action="send_notification",
        detail=f"Push sent to {'all' if target == 'all' else str(len(user_ids))} users — \"{title}\" ({sent} subscriptions reached)",
    ))
    db.commit()
    return RedirectResponse(
        url=f"/admin/?msg={quote(f'Notification sent to {sent} subscription(s)')}",
        status_code=303,
    )


# ─── Issue reporting (GitHub) ──────────────────────────────────────────────────

@router.post("/github")
async def update_github_settings(
    request: Request,
    github_repo: str = Form(""),
    github_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save the repo/token used by "Submit an Issue", verifying them first.

    Nothing is stored unless GitHub accepts the pair, so a typo cannot leave
    the feedback page looking configured while every report silently fails.
    A blank token means "keep the one already saved" — the page never shows
    the token, so it cannot be re-typed accurately.
    """
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    repo = github_repo.strip()
    token = github_token.strip()
    current = github_issues.get_config(db)

    if not repo:
        repo = current["repo"]
    if not token:
        token = current["token"]

    if not token:
        return RedirectResponse(
            url="/admin/?error=" + quote("Add a GitHub token — there isn't one saved yet.") + "#github",
            status_code=303,
        )

    ok, message = await github_issues.verify(token, repo)
    if not ok:
        return RedirectResponse(
            url="/admin/?error=" + quote(message) + "#github",
            status_code=303,
        )

    github_issues.save_config(db, token=token, repo=repo)
    db.add(AuditLog(
        user_id=admin.id,
        action="update_github_settings",
        detail=f"Issue reporting pointed at {repo} (token verified)",
    ))
    db.commit()
    return RedirectResponse(
        url="/admin/?msg=" + quote(f"Issue reporting saved — {message}") + "#github",
        status_code=303,
    )


@router.post("/github/test")
async def test_github_settings(request: Request, db: Session = Depends(get_db)):
    """Re-check the saved settings without changing them."""
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    config = github_issues.get_config(db)
    ok, message = await github_issues.verify(config["token"], config["repo"])
    key = "msg" if ok else "error"
    return RedirectResponse(
        url=f"/admin/?{key}=" + quote(message) + "#github",
        status_code=303,
    )


@router.post("/github/clear")
async def clear_github_settings(request: Request, db: Session = Depends(get_db)):
    """Forget the saved repo/token, falling back to the container's env vars."""
    admin = get_current_user(request, db)
    if not admin or admin.role != Role.admin:
        raise HTTPException(status_code=403)

    from urllib.parse import quote

    github_issues.clear_config(db)
    db.add(AuditLog(
        user_id=admin.id,
        action="clear_github_settings",
        detail="Cleared the saved issue-reporting token and repository",
    ))
    db.commit()
    return RedirectResponse(
        url="/admin/?msg=" + quote("Saved GitHub settings cleared.") + "#github",
        status_code=303,
    )
