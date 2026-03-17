"""
Awards router — displays season award standings and lets admins manage playoff teams.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_contributor
from app.database import get_db
from app.models import PlayoffTeam, Season, User
from app.services.awards import AWARD_REGISTRY, compute_all_awards, rank_award
from app.templates_config import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Public: award standings
# ---------------------------------------------------------------------------

@router.get("/awards", response_class=HTMLResponse)
async def awards_page(
    request: Request,
    season_year: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    all_seasons = db.query(Season).order_by(Season.year.desc()).all()

    if season_year:
        season = db.query(Season).filter(Season.year == season_year).first()
    else:
        season = db.query(Season).filter(Season.is_active == True).first()

    if not season:
        return templates.TemplateResponse(
            "dashboard/no_season.html", {"request": request, "user": user}
        )

    # Compute scores for every enabled award
    all_scores = compute_all_awards(db, season.id)

    # Build users_by_id map for ranking
    users_by_id: dict[int, User] = {
        u.id: u for u in db.query(User).filter(User.is_active == True).all()
    }

    # Build ranked results per award
    award_results = []
    for cfg in AWARD_REGISTRY:
        if not cfg.enabled:
            continue
        scores = all_scores.get(cfg.id, {})
        ranking = rank_award(scores, users_by_id, cfg.win_condition)
        award_results.append({
            "config": cfg,
            "ranking": ranking,
        })

    # Playoff teams for this season
    playoff_teams = [
        pt.team_abbreviation
        for pt in db.query(PlayoffTeam)
        .filter(PlayoffTeam.season_id == season.id)
        .order_by(PlayoffTeam.team_abbreviation)
        .all()
    ]

    return templates.TemplateResponse(
        "awards/awards.html",
        {
            "request":       request,
            "user":          user,
            "season":        season,
            "all_seasons":   all_seasons,
            "award_results": award_results,
            "playoff_teams": playoff_teams,
        },
    )


# ---------------------------------------------------------------------------
# Admin: manage playoff teams
# ---------------------------------------------------------------------------

@router.post("/admin/awards/playoff-teams/add")
async def add_playoff_team(
    request: Request,
    season_id: int = Form(...),
    team_abbreviation: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_contributor(request, db)
    team = team_abbreviation.strip().upper()
    if team:
        existing = (
            db.query(PlayoffTeam)
            .filter_by(season_id=season_id, team_abbreviation=team)
            .first()
        )
        if not existing:
            db.add(PlayoffTeam(season_id=season_id, team_abbreviation=team))
            db.commit()
    return RedirectResponse(
        url=f"/awards?season_year={(db.query(Season).get(season_id)).year}&msg=Team+added",
        status_code=303,
    )


@router.post("/admin/awards/playoff-teams/remove")
async def remove_playoff_team(
    request: Request,
    season_id: int = Form(...),
    team_abbreviation: str = Form(...),
    db: Session = Depends(get_db),
):
    admin = require_contributor(request, db)
    db.query(PlayoffTeam).filter_by(
        season_id=season_id, team_abbreviation=team_abbreviation
    ).delete()
    db.commit()
    season = db.query(Season).get(season_id)
    return RedirectResponse(
        url=f"/awards?season_year={season.year}&msg=Team+removed",
        status_code=303,
    )
