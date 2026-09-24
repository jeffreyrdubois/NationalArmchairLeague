from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Season, Week, Game, Pick, User, PushSubscription, Transaction, AppSetting
from app.auth import get_current_user, verify_password, hash_password
from app.services import mcp_tokens
from app.services.scoring import get_week_standings, get_season_standings
from app.services.visibility import (
    can_see_picks,
    get_submission_status,
    picks_are_revealed,
)

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

    # Fund summary for the current user
    fund_rows = {r.key: r.value for r in db.query(AppSetting).filter(
        AppSetting.key.in_(["entry_fee", "payment_venmo", "payment_paypal", "payment_cashapp", "payment_zelle"])
    ).all()}
    fund_entry_fee = float(fund_rows.get("entry_fee") or 0)
    fund_payment = {
        "venmo":   fund_rows.get("payment_venmo", ""),
        "paypal":  fund_rows.get("payment_paypal", ""),
        "cashapp": fund_rows.get("payment_cashapp", ""),
        "zelle":   fund_rows.get("payment_zelle", ""),
    }
    my_txns = db.query(Transaction).filter(Transaction.user_id == user.id).all()
    fund_my_paid_in  = sum(t.amount for t in my_txns if t.direction == "in")
    fund_my_received = sum(t.amount for t in my_txns if t.direction == "out")
    fund_my_balance  = fund_entry_fee - fund_my_paid_in

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
            "fund_entry_fee":  fund_entry_fee,
            "fund_my_paid_in": fund_my_paid_in,
            "fund_my_received": fund_my_received,
            "fund_my_balance": fund_my_balance,
            "fund_payment":    fund_payment,
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

    # Per-week breakdown. A week whose picks are still hidden has no scores
    # worth showing — every total is zero — so it carries submission status
    # instead: who has their picks in for the week, and who has not.
    week_data = []
    for week in weeks:
        revealed = picks_are_revealed(week)
        submission = [] if revealed else get_submission_status(db, week)
        week_data.append({
            "week": week,
            "revealed": revealed,
            "standings": get_week_standings(db, week.id) if revealed else [],
            "submission": submission,
            "submitted_count": sum(1 for r in submission if r["is_complete"]),
            "has_games": bool(submission and submission[0]["n_games"]),
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


@router.get("/profile/{user_id}", response_class=HTMLResponse)
async def user_profile(
    request: Request,
    user_id: int,
    season_year: int = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    profile_user = db.query(User).filter(User.id == user_id).first()
    if not profile_user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")

    if season_year:
        season = db.query(Season).filter(Season.year == season_year).first()
    else:
        season = db.query(Season).filter(Season.is_active == True).first()

    all_seasons = db.query(Season).order_by(Season.year.desc()).all()

    # One entry per week the profile's owner has picks in. Whether the picks
    # themselves travel to the template is decided here, not in the markup:
    # until a week locks, only the owner sees their own picks. Everyone else —
    # admins included — gets the count and nothing more.
    weeks_data = []
    total_pts = 0.0
    correct = 0
    wrong = 0
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
            if not picks:
                continue

            week_pts = sum(p.points_earned or 0 for p in picks)
            total_pts += week_pts
            correct += sum(1 for p in picks if p.is_correct is True)
            wrong += sum(1 for p in picks if p.is_correct is False)

            revealed = can_see_picks(week, user, profile_user.id)
            weeks_data.append({
                "week": week,
                "picks": picks if revealed else [],
                "revealed": revealed,
                "picks_made": len(picks),
                "week_points": week_pts,
            })

    return templates.TemplateResponse(
        "dashboard/profile.html",
        {
            "request": request,
            "user": user,
            "profile_user": profile_user,
            "season": season,
            "all_seasons": all_seasons,
            "weeks_data": weeks_data,
            "total_pts": total_pts,
            "correct": correct,
            "wrong": wrong,
        },
    )


def _public_url(request: Request, path: str) -> str:
    """An absolute URL as the outside world reaches this app.

    Behind the reverse proxy the app itself is spoken to over plain http, so
    the scheme the browser actually used comes from X-Forwarded-Proto.
    """
    url = request.base_url
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if proto in ("http", "https"):
        url = url.replace(scheme=proto)
    return str(url).rstrip("/") + path


def _render_settings(request: Request, db: Session, user: User, new_mcp_token: str = None):
    subscriptions = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
    msg = request.query_params.get("msg")
    error = request.query_params.get("error")
    may_use_mcp = mcp_tokens.may_hold_token(user)
    return templates.TemplateResponse(
        "account/settings.html",
        {
            "request": request,
            "user": user,
            "subscriptions": subscriptions,
            "msg": msg,
            "error": error,
            "may_use_mcp": may_use_mcp,
            "mcp_token": mcp_tokens.get_token(db, user) if may_use_mcp else None,
            "new_mcp_token": new_mcp_token,
            "mcp_url": _public_url(request, "/mcp"),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return _render_settings(request, db, user)


@router.post("/settings/mcp-token", response_class=HTMLResponse)
async def issue_mcp_token(request: Request, db: Session = Depends(get_db)):
    """Issue (or replace) the caller's MCP token.

    Rendered straight back rather than redirected: the token is readable this
    once and never again, and it must not travel in a URL.
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not mcp_tokens.may_hold_token(user):
        return RedirectResponse(url="/settings?error=Claude+access+is+not+available+for+your+account", status_code=303)
    token = mcp_tokens.issue_token(db, user)
    return _render_settings(request, db, user, new_mcp_token=token)


@router.post("/settings/mcp-token/revoke")
async def revoke_mcp_token(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    mcp_tokens.revoke_token(db, user)
    return RedirectResponse(url="/settings?msg=Claude+access+token+revoked", status_code=303)


@router.post("/settings/notifications")
async def save_notification_prefs(
    request: Request,
    notif_picks_reminder: str = Form(None),
    notif_week_results: str = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    user.notif_picks_reminder = notif_picks_reminder == "on"
    user.notif_week_results = notif_week_results == "on"
    db.commit()
    return RedirectResponse(url="/settings?msg=Notification+preferences+saved", status_code=303)


@router.post("/settings/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    if not verify_password(current_password, user.password_hash):
        return RedirectResponse(url="/settings?error=Current+password+is+incorrect", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/settings?error=New+password+must+be+at+least+8+characters", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/settings?error=New+passwords+do+not+match", status_code=303)

    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url="/settings?msg=Password+changed+successfully", status_code=303)
