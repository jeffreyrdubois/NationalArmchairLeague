"""
Scoring engine: determines winners against the spread and awards points.
"""
import logging
from sqlalchemy.orm import Session
from app.models import Game, Pick, Week, Season

logger = logging.getLogger(__name__)


def compute_home_covered(home_score: int, away_score: int, spread: float) -> bool:
    """
    Determine if the home team covered the spread.
    spread is from home team perspective: negative = home favored.
    Example: spread = -3.5 means home must win by 4+.
    Returns True if home covered, False if away covered.
    """
    margin = home_score - away_score  # positive = home winning
    # Home covers if margin > -spread (i.e., home_score - away_score > -spread)
    # e.g. spread=-3.5: home needs margin > 3.5
    return margin > -spread


def score_pick(pick: Pick, game: Game) -> None:
    """Update a single pick with correct/incorrect and points earned."""
    if not game.is_final or game.home_covered is None:
        return
    if game.home_covered:
        winner = game.home_team
    else:
        winner = game.away_team

    pick.is_correct = (pick.picked_team == winner)
    pick.points_earned = float(pick.confidence_points) if pick.is_correct else 0.0


def clear_pick_scores(db: Session, game: Game) -> int:
    """Reset every pick on a game back to pending. Returns how many changed."""
    reset = 0
    for pick in db.query(Pick).filter(Pick.game_id == game.id).all():
        if pick.is_correct is not None or pick.points_earned is not None:
            reset += 1
        pick.is_correct = None
        pick.points_earned = None
    return reset


def unscore_game(db: Session, game: Game) -> None:
    """Reset a game and all its picks back to an unscored/pending state.

    A pick may only carry a result while its game is final with a score on it,
    so anything that takes a game out of the final state has to come through
    here — otherwise the grid shows a game as "Upcoming" while the picks beside
    it are still painted right or wrong.
    """
    game.home_score = None
    game.away_score = None
    game.is_final = False
    game.is_in_progress = False
    game.quarter = None
    game.time_remaining = None
    game.home_covered = None
    clear_pick_scores(db, game)


def repair_pick_scoring(db: Session) -> int:
    """Clear results left on picks whose game is no longer final.

    Older versions let the score sync blank out a hand-entered final without
    touching the picks, which stranded games as "Upcoming" with red and green
    picks beside them. This puts those rows back to pending so the next score
    entry scores them cleanly. Returns the number of picks repaired.
    """
    stale_games = (
        db.query(Game)
        .join(Pick, Pick.game_id == Game.id)
        .filter(
            (Game.is_final == False) | (Game.is_final == None),  # noqa: E711,E712
            (Pick.is_correct != None) | (Pick.points_earned != None),  # noqa: E711
        )
        .distinct()
        .all()
    )
    repaired = 0
    for game in stale_games:
        repaired += clear_pick_scores(db, game)
        game.home_covered = None
    if repaired:
        db.commit()
        logger.warning(
            f"Repaired {repaired} pick(s) scored against "
            f"{len(stale_games)} game(s) that are not final — "
            "re-enter those scores to restore the results"
        )
    return repaired


def update_game_results(db: Session, game: Game) -> None:
    """
    After a game becomes final, compute coverage and score all picks for it.

    Safe to call again on an already-final game: coverage and every pick are
    recomputed from the current score, so correcting a score corrects the
    results that came from it.
    """
    if not game.is_final:
        return
    if game.home_score is None or game.away_score is None:
        return
    if game.spread is None:
        logger.warning(f"Game {game.id} is final but has no spread — cannot score picks")
        return

    game.home_covered = compute_home_covered(game.home_score, game.away_score, game.spread)
    db.flush()

    picks = db.query(Pick).filter(Pick.game_id == game.id).all()
    for pick in picks:
        score_pick(pick, game)

    db.commit()
    logger.info(
        f"Scored game {game.id} ({game.away_team}@{game.home_team}): "
        f"home_covered={game.home_covered}, picks={len(picks)}"
    )


def get_week_standings(db: Session, week_id: int) -> list[dict]:
    """Return leaderboard for a specific week."""
    week = db.query(Week).filter(Week.id == week_id).first()
    if not week:
        return []

    users_picks = {}
    picks = (
        db.query(Pick)
        .filter(Pick.week_id == week_id)
        .all()
    )
    for pick in picks:
        uid = pick.user_id
        if uid not in users_picks:
            users_picks[uid] = {
                "user": pick.user,
                "total": 0.0,
                "correct": 0,
                "wrong": 0,
                "pending": 0,
                "outstanding": 0.0,
            }
        if pick.is_correct is None:
            users_picks[uid]["pending"] += 1
            users_picks[uid]["outstanding"] += pick.confidence_points or 0
        elif pick.is_correct:
            users_picks[uid]["correct"] += 1
            users_picks[uid]["total"] += pick.points_earned or 0
        else:
            users_picks[uid]["wrong"] += 1

    # Potential = points already banked plus everything still up for grabs,
    # i.e. the most this player can finish the week with.
    for row in users_picks.values():
        row["potential"] = row["total"] + row["outstanding"]

    return sorted(
        users_picks.values(),
        key=lambda x: (x["total"], x["potential"]),
        reverse=True,
    )


def get_season_standings(db: Session, season_id: int) -> list[dict]:
    """Return season-long leaderboard."""
    picks = (
        db.query(Pick)
        .filter(Pick.season_id == season_id)
        .all()
    )

    users = {}
    for pick in picks:
        uid = pick.user_id
        if uid not in users:
            users[uid] = {
                "user": pick.user,
                "total": 0.0,
                "correct": 0,
                "wrong": 0,
                "pending": 0,
            }
        if pick.is_correct is None:
            users[uid]["pending"] += 1
        elif pick.is_correct:
            users[uid]["correct"] += 1
            users[uid]["total"] += pick.points_earned or 0
        else:
            users[uid]["wrong"] += 1

    return sorted(users.values(), key=lambda x: x["total"], reverse=True)
