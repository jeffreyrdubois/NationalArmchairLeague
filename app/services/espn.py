"""
ESPN unofficial API integration for NFL schedules and scores.
No API key required.
"""
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_CDN = "https://a.espncdn.com/i/teamlogos/nfl/500"
EASTERN = ZoneInfo("America/New_York")


def _parse_espn_date(date_str: str) -> Optional[datetime]:
    """Parse ESPN ISO date string to UTC datetime."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _round_spread(spread: float) -> float:
    """
    Round spread down to nearest 0.5 to avoid pushes.
    e.g. 4.0 -> 3.5, 3.5 -> 3.5, 7.0 -> 6.5
    """
    import math
    halves = math.floor(spread * 2)
    # If it's already a half-point, keep it; if whole number, subtract 0.5
    if halves % 2 == 0:  # whole number
        return (halves - 1) / 2.0
    return halves / 2.0


async def fetch_week_schedule(season_year: int, week: int) -> list[dict]:
    """
    Fetch NFL schedule for a given season year and week number.
    Returns a list of game dicts.
    """
    url = f"{ESPN_BASE}/scoreboard"
    params = {
        "seasontype": 2,  # regular season
        "season": season_year,
        "week": week,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"ESPN schedule fetch failed: {e}")
            return []

    data = resp.json()
    games = []
    events = data.get("events", [])

    for event in events:
        espn_id = event.get("id")
        kickoff_str = event.get("date")
        kickoff = _parse_espn_date(kickoff_str)
        competitions = event.get("competitions", [{}])
        comp = competitions[0] if competitions else {}

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)

        if not home or not away:
            continue

        home_team = home.get("team", {})
        away_team = away.get("team", {})

        status = event.get("status", {})
        status_type = status.get("type", {})

        home_score = None
        away_score = None
        is_final = status_type.get("completed", False)
        is_in_progress = status_type.get("state") == "in"

        if is_final or is_in_progress:
            try:
                home_score = int(home.get("score", 0))
                away_score = int(away.get("score", 0))
            except (ValueError, TypeError):
                pass

        # Determine who covered (only when final and spread is set)
        # We'll compute this in scoring service after spread is known

        quarter = status.get("period")
        time_remaining = status.get("displayClock")

        games.append({
            "espn_game_id": espn_id,
            "kickoff_time": kickoff,
            "home_team": home_team.get("abbreviation", ""),
            "home_team_name": home_team.get("displayName", ""),
            "home_team_logo": f"{ESPN_CDN}/{home_team.get('abbreviation', '').lower()}.png",
            "away_team": away_team.get("abbreviation", ""),
            "away_team_name": away_team.get("displayName", ""),
            "away_team_logo": f"{ESPN_CDN}/{away_team.get('abbreviation', '').lower()}.png",
            "home_score": home_score,
            "away_score": away_score,
            "is_final": is_final,
            "is_in_progress": is_in_progress,
            "quarter": str(quarter) if quarter else None,
            "time_remaining": time_remaining,
        })

    return games


async def fetch_live_scores(season_year: int, week: int) -> list[dict]:
    """Alias for schedule fetch — ESPN scoreboard includes live scores."""
    return await fetch_week_schedule(season_year, week)


async def fetch_current_week_info() -> dict:
    """
    Fetch ESPN's idea of the current NFL week.
    Returns {"season": int, "week": int, "season_type": int}
    """
    url = f"{ESPN_BASE}/scoreboard"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"ESPN current week fetch failed: {e}")
            return {}

    data = resp.json()
    season = data.get("season", {})
    week_obj = data.get("week", {})
    return {
        "season": season.get("year"),
        "week": week_obj.get("number"),
        "season_type": season.get("type"),
    }
