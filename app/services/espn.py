"""
ESPN unofficial API / nflverse data integration for NFL schedules and scores.

Historical schedule data comes from the nflverse games CSV hosted on GitHub,
which is reliably available for all completed seasons without API restrictions.

Live score updates attempt the ESPN scoreboard API first; if ESPN is unreachable
(e.g. IP restrictions in dev), they fall back to the nflverse data.
"""
import csv
import io
import asyncio
import httpx
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_CDN = "https://a.espncdn.com/i/teamlogos/nfl/500"
EASTERN = ZoneInfo("America/New_York")

NFLVERSE_GAMES_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
)

# In-process cache for the nflverse CSV rows.
#
# The cache has a short life on purpose. Without the expiry the fallback feed
# froze solid: the CSV was fetched once — usually at the first schedule sync,
# before the season — and every score sync for the rest of the container's
# life re-read that same snapshot, so scores nflverse published later never
# arrived. Whenever ESPN was unreachable that made scores stop populating
# entirely until someone restarted the app.
NFLVERSE_CACHE_TTL = timedelta(minutes=15)
_nflverse_cache: list[dict] | None = None
_nflverse_fetched_at: datetime | None = None

# nflverse uses a few different abbreviations from ESPN
NFLVERSE_TO_ESPN_ABBR: dict[str, str] = {
    "LA": "LAR",   # Los Angeles Rams
    "JAC": "JAX",  # Jacksonville Jaguars
    "WAS": "WSH",  # Washington Commanders
}

NFL_TEAM_NAMES: dict[str, str] = {
    "ARI": "Arizona Cardinals",   "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",   "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",  "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",      "DEN": "Denver Broncos",
    "DET": "Detroit Lions",       "GB":  "Green Bay Packers",
    "HOU": "Houston Texans",      "IND": "Indianapolis Colts",
    # both nflverse (JAC) and ESPN (JAX) spellings
    "JAC": "Jacksonville Jaguars", "JAX": "Jacksonville Jaguars",
    "KC":  "Kansas City Chiefs",
    # both nflverse (LA) and ESPN (LAR) spellings
    "LA":  "Los Angeles Rams",    "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams",    "LV":  "Las Vegas Raiders",
    "MIA": "Miami Dolphins",      "MIN": "Minnesota Vikings",
    "NE":  "New England Patriots","NO":  "New Orleans Saints",
    "NYG": "New York Giants",     "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",    "SF":  "San Francisco 49ers",
    "TB":  "Tampa Bay Buccaneers","TEN": "Tennessee Titans",
    # both nflverse (WAS) and ESPN (WSH) spellings
    "WAS": "Washington Commanders","WSH": "Washington Commanders",
}


def _parse_espn_date(date_str: str) -> Optional[datetime]:
    """Parse ESPN ISO date string to UTC datetime."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _parse_game_datetime(gameday: str, gametime: str | None) -> Optional[datetime]:
    """
    Build a UTC datetime from nflverse gameday (YYYY-MM-DD) and
    gametime (HH:MM, Eastern).  Returns None for unscheduled/TBD games.
    """
    if not gameday or gameday in ("", "NA", "nan"):
        return None
    try:
        if gametime and gametime not in ("", "NA", "nan", "None"):
            dt = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
        else:
            dt = datetime.strptime(gameday, "%Y-%m-%d")
        dt = dt.replace(tzinfo=EASTERN)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def _safe_int(val: str | None) -> int | None:
    if val in (None, "", "NA", "nan", "None"):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _round_spread(spread: float) -> float:
    """
    Round spread down to nearest 0.5 to avoid pushes.
    e.g. 4.0 -> 3.5, 3.5 -> 3.5, 7.0 -> 6.5
    """
    import math
    halves = math.floor(spread * 2)
    if halves % 2 == 0:  # whole number
        return (halves - 1) / 2.0
    return halves / 2.0


def _cache_is_fresh(now: datetime | None = None) -> bool:
    """True while the cached CSV is still young enough to serve."""
    if _nflverse_cache is None or _nflverse_fetched_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - _nflverse_fetched_at < NFLVERSE_CACHE_TTL


async def _load_nflverse_games() -> list[dict]:
    """
    Download and cache the nflverse games CSV from GitHub.
    Covers every NFL game from 1999 through the current season, scores included
    once each game is played.  ~2 MB; cached in-process for NFLVERSE_CACHE_TTL
    so a long-running container keeps picking up newly published scores.
    """
    global _nflverse_cache, _nflverse_fetched_at
    if _cache_is_fresh():
        return _nflverse_cache

    logger.info("Fetching nflverse games CSV from GitHub…")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(NFLVERSE_GAMES_URL)
            resp.raise_for_status()
    except Exception as e:
        if _nflverse_cache is not None:
            # A stale copy beats no schedule at all; try again next pass.
            logger.warning(f"nflverse refresh failed ({e}); serving cached copy")
            return _nflverse_cache
        raise

    _nflverse_cache = list(csv.DictReader(io.StringIO(resp.text)))
    _nflverse_fetched_at = datetime.now(timezone.utc)
    logger.info(f"Loaded {len(_nflverse_cache)} games from nflverse")
    return _nflverse_cache


async def fetch_week_schedule(season_year: int, week: int) -> list[dict]:
    """
    Fetch NFL schedule for a given season year and week number.
    Uses nflverse data (GitHub CSV) — works for every season without ESPN API
    access restrictions.  Returns a list of game dicts, empty on failure.
    """
    try:
        return await nflverse_week_rows(season_year, week)
    except Exception as e:
        logger.error(f"nflverse games fetch failed: {e}")
        return []


async def nflverse_week_rows(season_year: int, week: int) -> list[dict]:
    """The nflverse rows for one week, raising if the CSV cannot be read.

    ``fetch_week_schedule`` swallows the error; callers that need to report
    *why* a sync came back empty use this one.
    """
    all_games = await _load_nflverse_games()

    week_games = [
        g for g in all_games
        if g.get("season") == str(season_year)
        and g.get("week") == str(week)
        and g.get("game_type") == "REG"
    ]

    games = []
    for g in week_games:
        nfl_home = g.get("home_team", "")
        nfl_away = g.get("away_team", "")
        # Map to ESPN abbreviation for logos / downstream matching
        home_abbr = NFLVERSE_TO_ESPN_ABBR.get(nfl_home, nfl_home)
        away_abbr = NFLVERSE_TO_ESPN_ABBR.get(nfl_away, nfl_away)

        kickoff = _parse_game_datetime(g.get("gameday"), g.get("gametime"))

        home_score = _safe_int(g.get("home_score"))
        away_score = _safe_int(g.get("away_score"))
        is_final = home_score is not None and away_score is not None

        # nflverse 'espn' column holds the ESPN event ID (may be stored as float)
        raw_espn_id = g.get("espn", "")
        if raw_espn_id and raw_espn_id not in ("", "NA", "nan"):
            try:
                espn_id = str(int(float(raw_espn_id)))
            except (ValueError, TypeError):
                espn_id = raw_espn_id
        else:
            espn_id = g.get("game_id")  # fall back to nflverse game_id

        games.append({
            "espn_game_id": espn_id,
            "kickoff_time": kickoff,
            "home_team": home_abbr,
            "home_team_name": NFL_TEAM_NAMES.get(nfl_home, nfl_home),
            "home_team_logo": f"{ESPN_CDN}/{home_abbr.lower()}.png",
            "away_team": away_abbr,
            "away_team_name": NFL_TEAM_NAMES.get(nfl_away, nfl_away),
            "away_team_logo": f"{ESPN_CDN}/{away_abbr.lower()}.png",
            "home_score": home_score,
            "away_score": away_score,
            "is_final": is_final,
            "is_in_progress": False,  # nflverse is post-game data only
            "quarter": None,
            "time_remaining": None,
        })

    return games


def _parse_espn_scoreboard_events(events: list[dict]) -> list[dict]:
    """Parse the ESPN scoreboard 'events' array into standard game dicts."""
    games = []
    for event in events:
        espn_id = event.get("id")
        kickoff = _parse_espn_date(event.get("date"))
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
        is_final = status_type.get("completed", False)
        is_in_progress = status_type.get("state") == "in"

        home_score = None
        away_score = None
        if is_final or is_in_progress:
            try:
                home_score = int(home.get("score", 0))
                away_score = int(away.get("score", 0))
            except (ValueError, TypeError):
                pass

        quarter = status.get("period")
        time_remaining = status.get("displayClock")
        home_abbr = home_team.get("abbreviation", "")
        away_abbr = away_team.get("abbreviation", "")

        games.append({
            "espn_game_id": espn_id,
            "kickoff_time": kickoff,
            "home_team": home_abbr,
            "home_team_name": home_team.get("displayName", ""),
            "home_team_logo": f"{ESPN_CDN}/{home_abbr.lower()}.png",
            "away_team": away_abbr,
            "away_team_name": away_team.get("displayName", ""),
            "away_team_logo": f"{ESPN_CDN}/{away_abbr.lower()}.png",
            "home_score": home_score,
            "away_score": away_score,
            "is_final": is_final,
            "is_in_progress": is_in_progress,
            "quarter": str(quarter) if quarter else None,
            "time_remaining": time_remaining,
        })
    return games


async def fetch_live_scores(season_year: int, week: int) -> list[dict]:
    """
    Fetch live NFL scores.  Tries ESPN scoreboard first for real-time updates;
    if ESPN is unreachable, falls back to nflverse data (completed games only).
    """
    games, _meta = await fetch_live_scores_with_meta(season_year, week)
    return games


async def fetch_live_scores_with_meta(season_year: int, week: int) -> tuple[list[dict], dict]:
    """``fetch_live_scores`` plus a note on where the rows came from.

    Returns ``(games, meta)`` where meta is ``{"source", "error"}``. Source is
    ``"espn"`` for live data, ``"nflverse"`` for the post-game fallback, or
    None when neither answered.  The sync stores this so a week that never
    fills in says why — an unreachable ESPN and an empty feed look identical
    from the scores page otherwise.
    """
    meta: dict = {"source": None, "error": None}

    url = f"{ESPN_BASE}/scoreboard"
    params = {"seasontype": 2, "season": season_year, "week": week, "limit": 100}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=15, headers=headers) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])
            if events:
                meta["source"] = "espn"
                return _parse_espn_scoreboard_events(events), meta
            meta["error"] = "ESPN returned no games for this week"
        except Exception as e:
            meta["error"] = f"ESPN unreachable ({e}) — using nflverse, which only has finished games"
            logger.warning(f"ESPN live scores unavailable ({e}); falling back to nflverse")

    try:
        games = await nflverse_week_rows(season_year, week)
    except Exception as e:
        meta["error"] = f"{meta['error'] or 'ESPN returned nothing'}; nflverse also failed ({e})"
        return [], meta

    if games:
        meta["source"] = "nflverse"
    else:
        meta["error"] = f"{meta['error'] or 'ESPN returned nothing'}; nflverse has no rows for this week either"
    return games, meta


async def fetch_current_week_info() -> dict:
    """
    Fetch ESPN's idea of the current NFL week.
    Returns {"season": int, "week": int, "season_type": int}
    """
    url = f"{ESPN_BASE}/scoreboard"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        try:
            resp = await client.get(url, headers=headers)
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
