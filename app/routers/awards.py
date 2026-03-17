"""
Awards router — displays season award standings and lets contributors/admins
manage which teams have clinched or been eliminated from the playoffs.
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


# All 32 NFL teams, grouped by conference and division for display.
NFL_TEAMS = [
    # AFC
    {"abbr": "BUF", "name": "Buffalo Bills",         "conf": "AFC", "div": "East"},
    {"abbr": "MIA", "name": "Miami Dolphins",        "conf": "AFC", "div": "East"},
    {"abbr": "NE",  "name": "New England Patriots",  "conf": "AFC", "div": "East"},
    {"abbr": "NYJ", "name": "New York Jets",         "conf": "AFC", "div": "East"},
    {"abbr": "BAL", "name": "Baltimore Ravens",      "conf": "AFC", "div": "North"},
    {"abbr": "CIN", "name": "Cincinnati Bengals",    "conf": "AFC", "div": "North"},
    {"abbr": "CLE", "name": "Cleveland Browns",      "conf": "AFC", "div": "North"},
    {"abbr": "PIT", "name": "Pittsburgh Steelers",   "conf": "AFC", "div": "North"},
    {"abbr": "HOU", "name": "Houston Texans",        "conf": "AFC", "div": "South"},
    {"abbr": "IND", "name": "Indianapolis Colts",    "conf": "AFC", "div": "South"},
    {"abbr": "JAX", "name": "Jacksonville Jaguars",  "conf": "AFC", "div": "South"},
    {"abbr": "TEN", "name": "Tennessee Titans",      "conf": "AFC", "div": "South"},
    {"abbr": "DEN", "name": "Denver Broncos",        "conf": "AFC", "div": "West"},
    {"abbr": "KC",  "name": "Kansas City Chiefs",    "conf": "AFC", "div": "West"},
    {"abbr": "LV",  "name": "Las Vegas Raiders",     "conf": "AFC", "div": "West"},
    {"abbr": "LAC", "name": "Los Angeles Chargers",  "conf": "AFC", "div": "West"},
    # NFC
    {"abbr": "DAL", "name": "Dallas Cowboys",        "conf": "NFC", "div": "East"},
    {"abbr": "NYG", "name": "New York Giants",       "conf": "NFC", "div": "East"},
    {"abbr": "PHI", "name": "Philadelphia Eagles",   "conf": "NFC", "div": "East"},
    {"abbr": "WAS", "name": "Washington Commanders", "conf": "NFC", "div": "East"},
    {"abbr": "CHI", "name": "Chicago Bears",         "conf": "NFC", "div": "North"},
    {"abbr": "DET", "name": "Detroit Lions",         "conf": "NFC", "div": "North"},
    {"abbr": "GB",  "name": "Green Bay Packers",     "conf": "NFC", "div": "North"},
    {"abbr": "MIN", "name": "Minnesota Vikings",     "conf": "NFC", "div": "North"},
    {"abbr": "ATL", "name": "Atlanta Falcons",       "conf": "NFC", "div": "South"},
    {"abbr": "CAR", "name": "Carolina Panthers",     "conf": "NFC", "div": "South"},
    {"abbr": "NO",  "name": "New Orleans Saints",    "conf": "NFC", "div": "South"},
    {"abbr": "TB",  "name": "Tampa Bay Buccaneers",  "conf": "NFC", "div": "South"},
    {"abbr": "ARI", "name": "Arizona Cardinals",     "conf": "NFC", "div": "West"},
    {"abbr": "LAR", "name": "Los Angeles Rams",      "conf": "NFC", "div": "West"},
    {"abbr": "SF",  "name": "San Francisco 49ers",   "conf": "NFC", "div": "West"},
    {"abbr": "SEA", "name": "Seattle Seahawks",      "conf": "NFC", "div": "West"},
]

# Quick lookup: abbr → team dict
_TEAM_BY_ABBR = {t["abbr"]: t for t in NFL_TEAMS}

# Ordered division labels for display
_DIVISIONS = ["East", "North", "South", "West"]


def _group_teams():
    """Return teams grouped as {conf: {div: [team, ...]}}."""
    groups: dict[str, dict[str, list]] = {
        "AFC": {d: [] for d in _DIVISIONS},
        "NFC": {d: [] for d in _DIVISIONS},
    }
    for team in NFL_TEAMS:
        groups[team["conf"]][team["div"]].append(team)
    return groups


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

    all_scores = compute_all_awards(db, season.id)

    users_by_id: dict[int, User] = {
        u.id: u for u in db.query(User).filter(User.is_active == True).all()
    }

    award_results = []
    for cfg in AWARD_REGISTRY:
        if not cfg.enabled:
            continue
        scores = all_scores.get(cfg.id, {})
        ranking = rank_award(scores, users_by_id, cfg.win_condition)
        award_results.append({"config": cfg, "ranking": ranking})

    playoff_team_set: set[str] = {
        pt.team_abbreviation
        for pt in db.query(PlayoffTeam).filter_by(season_id=season.id).all()
    }

    return templates.TemplateResponse(
        "awards/awards.html",
        {
            "request":          request,
            "user":             user,
            "season":           season,
            "all_seasons":      all_seasons,
            "award_results":    award_results,
            "playoff_team_set": playoff_team_set,
            "team_groups":      _group_teams(),
            "divisions":        _DIVISIONS,
        },
    )


# ---------------------------------------------------------------------------
# Contributor/Admin: toggle a team's playoff status
# ---------------------------------------------------------------------------

@router.post("/admin/awards/playoff-teams/toggle", response_class=HTMLResponse)
async def toggle_playoff_team(
    request: Request,
    season_id: int = Form(...),
    team_abbreviation: str = Form(...),
    db: Session = Depends(get_db),
):
    require_contributor(request, db)

    abbr = team_abbreviation.strip().upper()
    existing = (
        db.query(PlayoffTeam)
        .filter_by(season_id=season_id, team_abbreviation=abbr)
        .first()
    )
    if existing:
        db.delete(existing)
        clinched = False
    else:
        db.add(PlayoffTeam(season_id=season_id, team_abbreviation=abbr))
        clinched = True
    db.commit()

    team = _TEAM_BY_ABBR.get(abbr, {"abbr": abbr, "name": abbr})

    # HTMX: return just the updated button partial
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            "awards/team_toggle.html",
            {
                "request":   request,
                "season_id": season_id,
                "abbr":      abbr,
                "name":      team["name"],
                "clinched":  clinched,
            },
        )

    # Non-HTMX fallback: redirect back to awards page
    season = db.query(Season).get(season_id)
    return RedirectResponse(url=f"/awards?season_year={season.year}", status_code=303)
