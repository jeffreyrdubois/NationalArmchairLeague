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


def update_game_results(db: Session, game: Game) -> None:
    """
    After a game becomes final, compute coverage and score all picks for it.
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
            }
        if pick.is_correct is None:
            users_picks[uid]["pending"] += 1
        elif pick.is_correct:
            users_picks[uid]["correct"] += 1
            users_picks[uid]["total"] += pick.points_earned or 0
        else:
            users_picks[uid]["wrong"] += 1

    return sorted(users_picks.values(), key=lambda x: x["total"], reverse=True)


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
