"""
Background scheduler for automatic data fetching.
- Every 15 min during game windows: fetch live scores
- Every hour outside game windows: fetch scores
- Tuesday morning: fetch new week schedule + spreads
- Spread lock enforced 24h before first kickoff
"""
import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import Season, Week, Game, SpreadSource
from app.services import espn, odds, scoring

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def get_active_week(db: Session):
    season = db.query(Season).filter(Season.is_active == True).first()
    if not season:
        return None, None
    now = datetime.utcnow()
    week = (
        db.query(Week)
        .filter(Week.season_id == season.id, Week.is_completed == False)
        .order_by(Week.week_number)
        .first()
    )
    return season, week


async def sync_scores():
    """Fetch live scores from ESPN and update the database."""
    db = SessionLocal()
    try:
        season, week = get_active_week(db)
        if not week or not week.espn_week:
            return
        if season.year == 9999:  # skip test season
            return

        logger.info(f"Syncing scores for season {season.year} week {week.week_number}")
        game_data = await espn.fetch_live_scores(season.year, week.espn_week)

        for gd in game_data:
            game = db.query(Game).filter(Game.espn_game_id == gd["espn_game_id"]).first()
            if not game:
                continue

            was_final = game.is_final
            game.home_score = gd["home_score"]
            game.away_score = gd["away_score"]
            game.is_final = gd["is_final"]
            game.is_in_progress = gd["is_in_progress"]
            game.quarter = gd["quarter"]
            game.time_remaining = gd["time_remaining"]

            if gd["is_final"] and not was_final:
                scoring.update_game_results(db, game)
            else:
                db.commit()

        # Check if all games in the week are final
        all_games = db.query(Game).filter(Game.week_id == week.id).all()
        if all_games and all(g.is_final for g in all_games):
            week.is_completed = True
            db.commit()
            logger.info(f"Week {week.week_number} is now completed")

    except Exception as e:
        logger.error(f"Score sync error: {e}")
        db.rollback()
    finally:
        db.close()


async def sync_week_schedule(season_year: int, week_number: int, espn_week: int) -> tuple[int, str | None]:
    """
    Fetch and store the schedule for a given week.
    Returns (game_count, error_message). error_message is None on success.
    """
    if season_year == 9999:
        return 0, "Cannot sync test season from ESPN"

    db = SessionLocal()
    try:
        season = db.query(Season).filter(Season.year == season_year).first()
        if not season:
            return 0, "Season not found"

        week = db.query(Week).filter(
            Week.season_id == season.id, Week.week_number == week_number
        ).first()
        if not week:
            return 0, "Week not found"

        game_data = await espn.fetch_week_schedule(season_year, espn_week)
        if not game_data:
            logger.warning(f"No games returned for week {week_number}")
            return 0, f"ESPN returned no games for {season_year} week {week_number} — the season may be over or the ESPN API may be temporarily unavailable"

        # Sort by kickoff to determine first game
        game_data.sort(key=lambda g: g["kickoff_time"] or datetime.max)
        if game_data[0]["kickoff_time"]:
            week.first_kickoff = game_data[0]["kickoff_time"]
            week.spread_lock_time = week.first_kickoff - timedelta(hours=24)
            week.espn_week = espn_week

        for gd in game_data:
            existing = db.query(Game).filter(Game.espn_game_id == gd["espn_game_id"]).first()
            if existing:
                # Update schedule info but preserve manual spreads
                existing.kickoff_time = gd["kickoff_time"]
                existing.home_team_name = gd["home_team_name"]
                existing.away_team_name = gd["away_team_name"]
            else:
                game = Game(
                    week_id=week.id,
                    espn_game_id=gd["espn_game_id"],
                    home_team=gd["home_team"],
                    away_team=gd["away_team"],
                    home_team_name=gd["home_team_name"],
                    away_team_name=gd["away_team_name"],
                    home_team_logo=gd["home_team_logo"],
                    away_team_logo=gd["away_team_logo"],
                    kickoff_time=gd["kickoff_time"],
                )
                db.add(game)

        db.commit()
        logger.info(f"Synced {len(game_data)} games for week {week_number}")
        return len(game_data), None

    except Exception as e:
        logger.error(f"Schedule sync error: {e}")
        db.rollback()
        return 0, str(e)
    finally:
        db.close()


async def sync_spreads():
    """Fetch spreads from The Odds API for unlocked weeks."""
    db = SessionLocal()
    try:
        season, week = get_active_week(db)
        if not week:
            return
        if season.year == 9999 or season.year < datetime.utcnow().year:  # skip test/historical
            return

        now = datetime.utcnow()
        if week.is_spreads_locked:
            return
        if week.spread_lock_time and now >= week.spread_lock_time:
            week.is_spreads_locked = True
            db.commit()
            logger.info(f"Spreads locked for week {week.week_number}")
            return

        spread_data = await odds.fetch_nfl_spreads()
        if not spread_data:
            return

        games = db.query(Game).filter(Game.week_id == week.id).all()
        updated = 0
        for game in games:
            # Don't overwrite manual overrides
            if game.spread_source == SpreadSource.manual and game.spread is not None:
                continue
            home_spread = odds.match_spread_to_game(
                game.home_team_name or game.home_team,
                game.away_team_name or game.away_team,
                spread_data,
            )
            if home_spread is not None:
                game.spread = home_spread
                game.spread_source = SpreadSource.api
                game.spread_updated_at = datetime.utcnow()
                updated += 1

        db.commit()
        logger.info(f"Updated spreads for {updated}/{len(games)} games in week {week.week_number}")

    except Exception as e:
        logger.error(f"Spread sync error: {e}")
        db.rollback()
    finally:
        db.close()


async def enforce_locks():
    """Check and enforce pick and spread locks based on time."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        weeks = db.query(Week).filter(Week.is_completed == False).all()
        for week in weeks:
            if week.spread_lock_time and now >= week.spread_lock_time and not week.is_spreads_locked:
                week.is_spreads_locked = True
                logger.info(f"Spread lock enforced for week {week.week_number}")
            if week.first_kickoff and now >= week.first_kickoff and not week.is_picks_locked:
                week.is_picks_locked = True
                logger.info(f"Pick lock enforced for week {week.week_number}")
        db.commit()
    except Exception as e:
        logger.error(f"Lock enforcement error: {e}")
        db.rollback()
    finally:
        db.close()


async def sync_historical_season(season_id: int, season_year: int, total_weeks: int = 18):
    """
    Populate games for every week of a historical season from ESPN.
    Runs sequentially to avoid DB write contention.
    Called as a background task — scores are intentionally not imported.
    """
    logger.info(f"Starting historical sync for {season_year} ({total_weeks} weeks)")
    total_games = 0
    for week_number in range(1, total_weeks + 1):
        count, error = await sync_week_schedule(season_year, week_number, week_number)
        if error:
            logger.warning(f"  week {week_number}: {error}")
        else:
            total_games += count
            logger.info(f"  week {week_number}: {count} games")
    logger.info(f"Historical sync for {season_year} complete — {total_games} games total")


def setup_scheduler():
    # Score sync: every 5 minutes
    scheduler.add_job(sync_scores, IntervalTrigger(minutes=5), id="sync_scores", replace_existing=True)
    # Spread sync: every 4 hours
    scheduler.add_job(sync_spreads, IntervalTrigger(hours=4), id="sync_spreads", replace_existing=True)
    # Lock enforcement: every minute
    scheduler.add_job(enforce_locks, IntervalTrigger(minutes=1), id="enforce_locks", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started")
