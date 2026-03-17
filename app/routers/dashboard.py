from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Season, Week, Game, Pick, User, PushSubscription, Transaction, AppSetting
from app.auth import get_current_user, verify_password, hash_password
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


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    subscriptions = db.query(PushSubscription).filter(PushSubscription.user_id == user.id).all()
    msg = request.query_params.get("msg")
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        "account/settings.html",
        {
            "request": request,
            "user": user,
            "subscriptions": subscriptions,
            "msg": msg,
            "error": error,
        },
    )


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
