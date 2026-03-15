"""
The Odds API integration for NFL spreads.
Free tier: 500 requests/month. We cache aggressively.
If no API key is set, spreads must be entered manually by contributors.
"""
import httpx
import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def round_spread_down(spread: float) -> float:
    """
    Round spread down to nearest 0.5 to avoid pushes.
    e.g. 4.0 -> 3.5, 3.5 -> 3.5, -4.0 -> -3.5, -3.5 -> -3.5
    """
    if spread == 0:
        return 0.0
    sign = 1 if spread > 0 else -1
    abs_spread = abs(spread)
    halves = math.floor(abs_spread * 2)
    if halves % 2 == 0:  # whole number, e.g. 4.0 -> floor(8)/2=4, subtract 0.5
        rounded = (halves - 1) / 2.0
    else:
        rounded = halves / 2.0
    # Ensure we have at least 0.5
    rounded = max(0.5, rounded)
    return sign * rounded


async def fetch_nfl_spreads() -> list[dict]:
    """
    Fetch current NFL game spreads from The Odds API.
    Returns list of dicts with home_team, away_team, home_spread.
    home_spread is from the home team's perspective (negative = home favored).
    """
    if not ODDS_API_KEY:
        logger.warning("ODDS_API_KEY not set — spreads must be entered manually")
        return []

    url = f"{ODDS_API_BASE}/sports/americanfootball_nfl/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads",
        "oddsFormat": "american",
        "bookmakers": "draftkings",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            remaining = resp.headers.get("x-requests-remaining", "?")
            logger.info(f"Odds API requests remaining: {remaining}")
        except Exception as e:
            logger.error(f"Odds API fetch failed: {e}")
            return []

    results = []
    for game in resp.json():
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        commence = game.get("commence_time", "")

        home_spread = None
        for bookmaker in game.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "spreads":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == home:
                        raw = outcome.get("point", 0)
                        home_spread = round_spread_down(raw)
                        break
                if home_spread is not None:
                    break
            if home_spread is not None:
                break

        results.append({
            "home_team": home,
            "away_team": away,
            "home_spread": home_spread,
            "commence_time": commence,
        })

    return results


def match_spread_to_game(game_home: str, game_away: str, spreads: list[dict]) -> Optional[float]:
    """
    Match a spread from the API results to a game by team name.
    The Odds API uses full team names, ESPN uses abbreviations — we match by
    checking if the ESPN abbreviation appears in the full name.
    """
    game_home_lower = game_home.lower()
    game_away_lower = game_away.lower()

    for s in spreads:
        h = s["home_team"].lower()
        a = s["away_team"].lower()
        if (game_home_lower in h or h in game_home_lower) and \
           (game_away_lower in a or a in game_away_lower):
            return s["home_spread"]

    return None
