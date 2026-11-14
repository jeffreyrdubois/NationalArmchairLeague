"""What-if scoring for unfinished games in a week.

The Simulator page lets a player assume which side covers each remaining
game and see how the week standings would land. Finished games stay on
their real results; nothing is written to the database.
"""
from __future__ import annotations

from app.models import Game, Pick, User


def covering_team(game: Game, home_covered: bool) -> str:
    """The team abbreviation that covers under ``home_covered``."""
    return game.home_team if home_covered else game.away_team


def default_home_covered(game: Game, viewer_pick: Pick | None) -> bool:
    """Assume the outcome that awards the viewer's pick on this game.

    If the viewer has no pick, home covering is the neutral starting point
    so every remaining game still has a toggleable assumption.
    """
    if viewer_pick is None:
        return True
    if viewer_pick.picked_team == game.home_team:
        return True
    if viewer_pick.picked_team == game.away_team:
        return False
    return True


def default_assumptions(
    games: list[Game],
    viewer_picks: dict[int, Pick],
) -> dict[int, bool]:
    """``game_id -> home_covered`` for every non-final game.

    Defaults so the viewing player wins each remaining game they picked —
    the starting slate the Simulator page opens on.
    """
    assumed: dict[int, bool] = {}
    for game in games:
        if game.is_final:
            continue
        assumed[game.id] = default_home_covered(game, viewer_picks.get(game.id))
    return assumed


def points_for_pick(pick: Pick | None, game: Game, home_covered: bool) -> float:
    """Confidence points this pick earns if ``home_covered`` is the result."""
    if pick is None:
        return 0.0
    winner = covering_team(game, home_covered)
    if pick.picked_team == winner:
        return float(pick.confidence_points or 0)
    return 0.0


def simulate_week(
    games: list[Game],
    users: list[User],
    pick_matrix: dict[int, dict[int, Pick]],
    assumptions: dict[int, bool],
) -> list[dict]:
    """Recompute week standings under assumed covers for unfinished games.

    ``assumptions`` maps ``game_id -> home_covered`` for non-final games.
    Finals always use ``game.home_covered``. Rows are sorted by simulated
    total descending, then by user id for a stable podium.
    """
    rows: list[dict] = []
    for user in users:
        total = 0.0
        correct = 0
        wrong = 0
        pending = 0
        per_game: dict[int, float] = {}
        picks = pick_matrix.get(user.id, {})

        for game in games:
            pick = picks.get(game.id)
            if game.is_final:
                if game.home_covered is None:
                    # Unscored final — treat like pending with no award.
                    pending += 1
                    per_game[game.id] = 0.0
                    continue
                home_covered = bool(game.home_covered)
            else:
                if game.id not in assumptions:
                    pending += 1
                    per_game[game.id] = 0.0
                    continue
                home_covered = assumptions[game.id]

            earned = points_for_pick(pick, game, home_covered)
            per_game[game.id] = earned
            total += earned
            if pick is None:
                continue
            if earned > 0:
                correct += 1
            else:
                wrong += 1

        rows.append({
            "user": user,
            "total": total,
            "correct": correct,
            "wrong": wrong,
            "pending": pending,
            "per_game": per_game,
        })

    rows.sort(key=lambda r: (-r["total"], r["user"].id))
    return rows


def podium_ranks(rows: list[dict]) -> dict[int, int]:
    """Map ``user_id -> place`` (1, 2, or 3) for the top three distinct totals.

    Ties share a place and consume the next slots the way a podium would
    (two tied for first means the next place is third).
    """
    ranks: dict[int, int] = {}
    if not rows:
        return ranks
    place = 1
    i = 0
    while i < len(rows) and place <= 3:
        score = rows[i]["total"]
        tied = [r for r in rows[i:] if r["total"] == score]
        for row in tied:
            if place <= 3:
                ranks[row["user"].id] = place
        i += len(tied)
        place += len(tied)
    return ranks
