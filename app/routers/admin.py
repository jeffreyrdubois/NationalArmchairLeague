from app.templates_config import templates
"""
Admin and Contributor routes.
- Contributors: update spreads and scores manually
- Admins: all of the above + manage users, edit any pick, manage seasons/weeks
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Season, Week, Game, Pick, User, Role, AuditLog, SpreadSource
from app.auth import get_current_user, require_contributor, require_admin
from app.services import espn
from app.services.scoring import update_game_results

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
    if season:
        weeks = (
            db.query(Week)
            .filter(Week.season_id == season.id)
            .order_by(Week.week_number)
            .all()
        )

    # Default to current open week
    current_week = next((w for w in weeks if not w.is_completed), None)
    games = []
    selected_week = None
    if current_week:
        selected_week = current_week
        games = (
            db.query(Game)
            .filter(Game.week_id == current_week.id)
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
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    week = db.query(Week).filter(Week.id == game.week_id).first()
    if week and week.is_spreads_locked:
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
    return RedirectResponse(url="/admin/spreads", status_code=303)


@router.post("/scores/update")
async def update_score(
    request: Request,
    game_id: int = Form(...),
    home_score: int = Form(...),
    away_score: int = Form(...),
    is_final: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        raise HTTPException(status_code=403)

    game = db.query(Game).filter(Game.id == game_id).first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    old_final = game.is_final
    game.home_score = home_score
    game.away_score = away_score
    game.is_final = is_final

    log = AuditLog(
        user_id=user.id,
        action="update_score",
        target_type="game",
        target_id=game_id,
        detail=f"Score set to {away_score}@{home_score}, final={is_final}",
    )
    db.add(log)

    if is_final and not old_final:
        update_game_results(db, game)
    else:
        db.commit()

    return RedirectResponse(url="/admin/scores", status_code=303)


@router.get("/scores", response_class=HTMLResponse)
async def scores_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role not in (Role.contributor, Role.admin):
        return RedirectResponse(url="/", status_code=303)

    season = db.query(Season).filter(Season.is_active == True).first()
    current_week = None
    games = []
    if season:
        current_week = (
            db.query(Week)
            .filter(Week.season_id == season.id, Week.is_completed == False)
            .order_by(Week.week_number)
            .first()
        )
        if current_week:
            games = (
                db.query(Game)
                .filter(Game.week_id == current_week.id)
                .order_by(Game.kickoff_time)
                .all()
            )

    return templates.TemplateResponse(
        "admin/scores.html",
        {
            "request": request,
            "user": user,
            "season": season,
            "current_week": current_week,
            "games": games,
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

    return templates.TemplateResponse(
        "admin/home.html",
        {
            "request": request,
            "user": user,
            "seasons": seasons,
            "season_weeks": season_weeks,
            "users": users,
            "recent_logs": recent_logs,
        },
    )


@router.post("/season/create")
async def create_season(
    request: Request,
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

    log = AuditLog(user_id=user.id, action="create_season", target_type="season",
                   target_id=season.id, detail=f"Created season {year} with 18 weeks")
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
    await sync_week_schedule(season.year, week.week_number, week.espn_week or week.week_number)

    return RedirectResponse(url=f"/admin/week/{week_id}", status_code=303)


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

    for game_id, (points, team) in new_picks.items():
        existing = db.query(Pick).filter(
            Pick.user_id == user_id, Pick.game_id == game_id
        ).first()
        if existing:
            old = f"{existing.picked_team}/{existing.confidence_points}"
            existing.confidence_points = points
            existing.picked_team = team
            existing.is_correct = None
            existing.points_earned = None
            detail = f"Changed from {old} to {team}/{points}"
        else:
            existing = Pick(
                user_id=user_id,
                game_id=game_id,
                week_id=week_id,
                season_id=week.season_id,
                picked_team=team,
                confidence_points=points,
            )
            db.add(existing)
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
async def users_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user or user.role != Role.admin:
        return RedirectResponse(url="/", status_code=303)

    users = db.query(User).order_by(User.last_name, User.first_name).all()
    return templates.TemplateResponse(
        "admin/users.html",
        {"request": request, "user": user, "users": users, "roles": Role},
    )


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
    return RedirectResponse(url="/admin/users", status_code=303)


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
    return RedirectResponse(url="/admin/users", status_code=303)
