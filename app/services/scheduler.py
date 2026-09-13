"""
Background scheduler for automatic data fetching.
- Every 15 min during game windows: fetch live scores
- Every hour outside game windows: fetch scores
- Tuesday morning: fetch new week schedule + spreads
- Spread lock enforced 24h before first kickoff
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import AppSetting, Season, Week, Game, ScoreSource, SpreadSource, Pick, User
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


def week_kickoff(db: Session, week: Week) -> datetime | None:
    """When this week starts, from the week row or its earliest game.

    ``first_kickoff`` is only written by a schedule sync, so a week whose games
    arrived another way has none — and a week with no kickoff used to be left
    out of the score sync entirely, silently, for the whole season.
    """
    if week.first_kickoff:
        return week.first_kickoff
    kickoffs = [
        g.kickoff_time
        for g in db.query(Game).filter(Game.week_id == week.id).all()
        if g.kickoff_time is not None
    ]
    return min(kickoffs) if kickoffs else None


def get_syncable_weeks(db: Session, season: Season) -> list[Week]:
    """Every unfinished week of a season that has already kicked off.

    Deliberately not just the first unfinished week: one week left open by a
    game the feed never reports (a moved game, an ID that no longer matches)
    used to block score syncing for every week after it, so the current week's
    games sat on "Upcoming" all Sunday.

    A missing ``espn_week`` no longer disqualifies a week either — the week
    number is the same thing in every regular-season case, and requiring the
    column meant a week set up by hand never synced a single score while its
    spreads, which are matched on team name, filled in normally.
    """
    now = datetime.utcnow()
    weeks = (
        db.query(Week)
        .filter(
            Week.season_id == season.id,
            Week.is_completed == False,  # noqa: E712
        )
        .order_by(Week.week_number)
        .all()
    )
    syncable = []
    for week in weeks:
        kickoff = week_kickoff(db, week)
        if kickoff is not None and kickoff <= now:
            syncable.append(week)
    return syncable


def _norm_team(abbr: str | None) -> str:
    """Team abbreviation in ESPN's spelling, for comparing feed to database."""
    a = (abbr or "").strip().upper()
    return espn.NFLVERSE_TO_ESPN_ABBR.get(a, a)


def match_feed_row_to_game(
    db: Session, week: Week, gd: dict, games: list[Game] | None = None
) -> Game | None:
    """Find the stored game a feed row is about, by id or by matchup.

    Matching on the feed's game id alone is brittle. A week imported before
    nflverse had ESPN's event ids stored the nflverse ``2026_02_DET_BUF``
    style id instead, and that never equals the numeric id the live feed
    sends — so every score for that week was dropped on the floor while
    spreads, matched on team name, kept filling in. That is exactly the
    "spreads populate, scores don't" shape.

    So: try the id, then fall back to the matchup within the week, and write
    the feed's id back onto the row so later passes match directly.
    """
    feed_id = str(gd.get("espn_game_id") or "").strip()
    if feed_id:
        game = db.query(Game).filter(Game.espn_game_id == feed_id).first()
        if game:
            # An id belonging to another week is not this week's game.
            return game if game.week_id == week.id else None

    home, away = _norm_team(gd.get("home_team")), _norm_team(gd.get("away_team"))
    if not home or not away:
        return None
    if games is None:
        games = db.query(Game).filter(Game.week_id == week.id).all()

    for game in games:
        if _norm_team(game.home_team) == home and _norm_team(game.away_team) == away:
            taken = any(g is not game and g.espn_game_id == feed_id for g in games)
            if feed_id and game.espn_game_id != feed_id and not taken:
                logger.info(
                    f"Week {week.week_number}: {away}@{home} matched by matchup; "
                    f"updating game id {game.espn_game_id} -> {feed_id}"
                )
                game.espn_game_id = feed_id
            return game
    return None


def apply_feed_scores(db: Session, game: Game, gd: dict) -> bool:
    """Apply one feed row to a game, refusing anything that loses information.

    The feed is only ever allowed to *add* what it knows. It may not blank a
    score, un-final a finished game, or overwrite what a contributor typed in
    by hand — all three used to happen every five minutes whenever ESPN was
    unreachable and the nflverse fallback had not published the week yet, which
    is what made hand-entered finals vanish and finished games read "Upcoming".

    Returns True if the game was changed.
    """
    if game.score_source == ScoreSource.manual:
        return False  # a human entered this; the feed does not get a vote

    has_scores = gd["home_score"] is not None and gd["away_score"] is not None
    if not has_scores:
        return False  # feed has nothing for this game yet — leave it alone
    if game.is_final and not gd["is_final"]:
        return False  # a finished game does not come back to life

    was_final = game.is_final
    game.home_score = gd["home_score"]
    game.away_score = gd["away_score"]
    game.is_final = gd["is_final"]
    game.is_in_progress = gd["is_in_progress"]
    game.quarter = gd["quarter"]
    game.time_remaining = gd["time_remaining"]
    game.score_source = ScoreSource.api
    game.score_updated_at = datetime.utcnow()

    if gd["is_final"] and not was_final:
        scoring.update_game_results(db, game)
    else:
        db.commit()
    return True


def complete_week_if_done(db: Session, week: Week) -> bool:
    """Mark a week complete once every game is final, and notify. """
    all_games = db.query(Game).filter(Game.week_id == week.id).all()
    if not all_games or not all(g.is_final for g in all_games):
        return False

    week.is_completed = True
    db.commit()
    logger.info(f"Week {week.week_number} is now completed")
    # Fire week-results push notifications
    try:
        from app.services.notifications import send_to_all
        sent = send_to_all(
            title="Week Results Are In!",
            body=f"Week {week.week_number} is complete — check the standings.",
            url="/standings",
            notif_filter="notif_week_results",
        )
        logger.info(f"Sent week-results push to {sent} subscriptions")
    except Exception as ne:
        logger.warning(f"Week-results push error: {ne}")
    return True


SCORE_SYNC_STATUS_KEY = "score_sync_status"


def record_score_sync_status(db: Session, status: dict) -> None:
    """Store what the last score sync did, so the scores page can show it.

    Every way the score sync can come up empty — ESPN unreachable, a week the
    feed has no rows for, game ids that no longer line up — used to be a line
    in the container log and nothing else, which is why "the scores aren't
    populating" had no answer short of reading the code.
    """
    try:
        db.merge(AppSetting(key=SCORE_SYNC_STATUS_KEY, value=json.dumps(status)))
        db.commit()
    except Exception as e:  # never let bookkeeping break the sync
        logger.warning(f"Could not record score sync status: {e}")
        db.rollback()


def get_score_sync_status(db: Session) -> dict | None:
    """The stored status of the last score sync, with ran_at as a datetime."""
    row = db.query(AppSetting).filter(AppSetting.key == SCORE_SYNC_STATUS_KEY).first()
    if not row or not row.value:
        return None
    try:
        status = json.loads(row.value)
    except ValueError:
        return None
    try:
        status["ran_at"] = datetime.fromisoformat(status["ran_at"])
    except (KeyError, TypeError, ValueError):
        status["ran_at"] = None
    return status


async def sync_week_scores(db: Session, season: Season, week: Week) -> dict:
    """Pull the feed for one week and apply everything it has to say.

    Returns a summary: which feed answered, how many of the week's games the
    feed actually matched, and how many rows changed.
    """
    games = db.query(Game).filter(Game.week_id == week.id).all()
    summary = {
        "week": week.week_number,
        "source": None,
        "endpoint": None,
        "error": None,
        "games": len(games),
        "matched": 0,
        "updated": 0,
        "unmatched": [],
    }

    espn_week = week.espn_week or week.week_number
    try:
        game_data, meta = await espn.fetch_live_scores_with_meta(season.year, espn_week)
    except Exception as fe:
        logger.warning(f"Score fetch failed for week {week.week_number}: {fe}")
        summary["error"] = str(fe)
        return summary

    summary["source"] = meta.get("source")
    summary["endpoint"] = meta.get("endpoint")
    summary["error"] = meta.get("error")

    matched_ids = set()
    for gd in game_data:
        game = match_feed_row_to_game(db, week, gd, games)
        if not game:
            continue
        matched_ids.add(game.id)
        if apply_feed_scores(db, game, gd):
            summary["updated"] += 1

    summary["matched"] = len(matched_ids)
    summary["unmatched"] = [
        f"{g.away_team}@{g.home_team}" for g in games if g.id not in matched_ids
    ]
    db.commit()   # persist any game ids healed by matching on the matchup

    complete_week_if_done(db, week)
    return summary


async def sync_scores(trigger: str = "scheduled") -> dict:
    """Fetch live scores from ESPN and update every week still in play."""
    db = SessionLocal()
    status = {"ran_at": datetime.utcnow().isoformat(), "trigger": trigger, "weeks": []}
    try:
        season = db.query(Season).filter(Season.is_active == True).first()  # noqa: E712
        if not season:
            status["error"] = "No active season"
            return status
        if season.year == 9999:  # the test season has no feed
            status["error"] = "Test season — nothing to sync"
            return status

        weeks = get_syncable_weeks(db, season)
        if not weeks:
            status["error"] = "No week has kicked off yet"

        for week in weeks:
            logger.info(f"Syncing scores for season {season.year} week {week.week_number}")
            status["weeks"].append(await sync_week_scores(db, season, week))

        return status

    except Exception as e:
        logger.error(f"Score sync error: {e}")
        db.rollback()
        status["error"] = str(e)
        return status
    finally:
        record_score_sync_status(db, status)
        db.close()


async def sync_one_week_scores(week_id: int) -> dict:
    """Score sync for a single week, for the admin "Sync Scores" button."""
    db = SessionLocal()
    status = {"ran_at": datetime.utcnow().isoformat(), "trigger": "manual", "weeks": []}
    try:
        week = db.query(Week).filter(Week.id == week_id).first()
        if not week:
            status["error"] = "Week not found"
            return status
        season = db.query(Season).filter(Season.id == week.season_id).first()
        if not season:
            status["error"] = "Season not found"
            return status
        if season.year == 9999:
            status["error"] = "Cannot sync scores for the test season"
            return status

        status["weeks"].append(await sync_week_scores(db, season, week))
        return status
    except Exception as e:
        logger.error(f"Manual score sync error: {e}")
        db.rollback()
        status["error"] = str(e)
        return status
    finally:
        record_score_sync_status(db, status)
        db.close()


def describe_sync_summary(summary: dict) -> str:
    """One line a contributor can act on, for the flash message."""
    source = {"espn": "ESPN", "nflverse": "nflverse"}.get(summary.get("source"), "no feed")
    if summary.get("source") == "espn" and summary.get("endpoint"):
        source = f"ESPN ({summary['endpoint']})"
    parts = [
        f"{source}: matched {summary.get('matched', 0)} of {summary.get('games', 0)} games, "
        f"updated {summary.get('updated', 0)}"
    ]
    if summary.get("unmatched"):
        shown = ", ".join(summary["unmatched"][:4])
        more = len(summary["unmatched"]) - 4
        parts.append(f"no feed row for {shown}{f' and {more} more' if more > 0 else ''}")
    if summary.get("error"):
        parts.append(summary["error"])
    return " — ".join(parts)


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

        week_games = db.query(Game).filter(Game.week_id == week.id).all()
        for gd in game_data:
            # By id first, then by matchup: a week imported before nflverse
            # carried ESPN's event ids holds ids the feed no longer sends, and
            # matching on id alone inserted a second copy of the same game —
            # leaving the picks on the old row and the scores on the new one.
            existing = (
                db.query(Game).filter(Game.espn_game_id == gd["espn_game_id"]).first()
                or match_feed_row_to_game(db, week, gd, week_games)
            )
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


async def sync_week_spreads(week_id: int) -> tuple[int, str | None]:
    """
    Manually fetch spreads from The Odds API for a single week.
    Mirrors sync_spreads() but targets one week and reports a count, for the
    admin "Sync Odds" button. Returns (updated_count, error_message);
    error_message is None on success. Manual spread overrides are preserved.
    """
    db = SessionLocal()
    try:
        week = db.query(Week).filter(Week.id == week_id).first()
        if not week:
            return 0, "Week not found"
        season = db.query(Season).filter(Season.id == week.season_id).first()
        if season and season.year == 9999:
            return 0, "Cannot sync odds for the test season"

        spread_data = await odds.fetch_nfl_spreads()
        if not spread_data:
            return 0, (
                "No spreads returned — make sure ODDS_API_KEY is set, or The Odds "
                "API may currently have no lines for these games (common in the "
                "offseason)."
            )

        games = db.query(Game).filter(Game.week_id == week_id).all()
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
        logger.info(f"Manual odds sync updated {updated} game(s) for week {week.week_number}")
        return updated, None
    except Exception as e:
        logger.error(f"Manual spread sync error: {e}")
        db.rollback()
        return 0, str(e)
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
                if week.picks_lock_override:
                    pass  # admin manually unlocked; don't auto-relock
                else:
                    week.is_picks_locked = True
                    logger.info(f"Pick lock enforced for week {week.week_number}")
        db.commit()
    except Exception as e:
        logger.error(f"Lock enforcement error: {e}")
        db.rollback()
    finally:
        db.close()


async def send_picks_reminders():
    """
    Two hours before picks lock, push a reminder to users who haven't
    submitted all their picks yet.  Fires at most once per week.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        window_start = now
        window_end = now + timedelta(hours=2)

        weeks = (
            db.query(Week)
            .filter(
                Week.is_completed == False,
                Week.picks_reminder_sent == False,
                Week.first_kickoff != None,
                Week.first_kickoff > window_start,
                Week.first_kickoff <= window_end,
            )
            .all()
        )
        for week in weeks:
            game_count = db.query(Game).filter(Game.week_id == week.id).count()
            if game_count == 0:
                continue

            # Collect users who haven't submitted a full set of picks
            all_users = db.query(User).filter(User.is_active == True,
                                              User.notif_picks_reminder == True).all()
            targets = []
            for u in all_users:
                pick_count = db.query(Pick).filter(
                    Pick.user_id == u.id, Pick.week_id == week.id
                ).count()
                if pick_count < game_count:
                    targets.append(u)

            if targets:
                from app.services.notifications import send_to_all, send_to_user
                for u in targets:
                    send_to_user(
                        user=u,
                        title="Picks close soon!",
                        body=f"Week {week.week_number} picks lock in under 2 hours.",
                        url="/picks",
                        db=db,
                    )
                logger.info(f"Sent picks reminder for week {week.week_number} to {len(targets)} users")

            week.picks_reminder_sent = True
        db.commit()
    except Exception as e:
        logger.error(f"Picks reminder error: {e}")
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
    ok_weeks = []
    failed_weeks = []
    for week_number in range(1, total_weeks + 1):
        count, error = await sync_week_schedule(season_year, week_number, week_number)
        if error:
            failed_weeks.append(week_number)
            logger.warning(f"  week {week_number}: {error}")
        else:
            total_games += count
            ok_weeks.append(week_number)
            logger.info(f"  week {week_number}: {count} games")
    logger.info(
        f"Historical sync for {season_year} complete — "
        f"{total_games} games across {len(ok_weeks)} weeks "
        f"({len(failed_weeks)} weeks unavailable: {failed_weeks or 'none'})"
    )


def setup_scheduler():
    # Score sync: every 5 minutes
    scheduler.add_job(sync_scores, IntervalTrigger(minutes=5), id="sync_scores", replace_existing=True)
    # Spread sync: every 4 hours
    scheduler.add_job(sync_spreads, IntervalTrigger(hours=4), id="sync_spreads", replace_existing=True)
    # Lock enforcement: every minute
    scheduler.add_job(enforce_locks, IntervalTrigger(minutes=1), id="enforce_locks", replace_existing=True)
    # Picks reminder push: every 5 minutes (low cost; only fires once per week)
    scheduler.add_job(send_picks_reminders, IntervalTrigger(minutes=5), id="picks_reminders", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started")
