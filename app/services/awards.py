"""
Generic configurable award rule engine for National Armchair League.

Adding a new award:
  1. Append an AwardConfig to AWARD_REGISTRY.
  2. If the award needs a new aggregation strategy, add it to AggregationType
     and implement a handler in compute_award().
  3. If the award needs a new filter field, add it to _build_pick_contexts().

No other code needs to change.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models import Game, Pick, PlayoffTeam, User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule configuration primitives
# ---------------------------------------------------------------------------

class FilterOperator(str, Enum):
    EQ     = "eq"      # field == value
    LTE    = "lte"     # field <= value
    GTE    = "gte"     # field >= value
    IN     = "in"      # field in value  (value must be a list/set)
    NOT_IN = "not_in"  # field not in value


class AggregationType(str, Enum):
    COUNT          = "count"           # count qualifying picks
    SUM            = "sum"             # sum aggregate_field of qualifying picks
    COMPLETE_SETS  = "complete_sets"   # min count across all group_values
    CONTRARIAN     = "contrarian"      # pairwise positive-distance calculation


@dataclass
class FilterConfig:
    """A single predicate applied to a pick-context dict."""
    field: str
    op: FilterOperator
    value: Any


@dataclass
class AwardConfig:
    """Full specification for one award."""
    id: str
    name: str
    description: str
    aggregation: AggregationType

    # Filters applied before aggregation (all must pass — logical AND)
    filters: list[FilterConfig] = field(default_factory=list)

    # SUM: which context field to sum
    aggregate_field: str | None = None

    # COMPLETE_SETS: group picks by this field and check all expected values exist
    group_field: str | None = None
    group_values: list[Any] | None = None

    # "most" → highest score wins; "least" → lowest score wins
    win_condition: str = "most"

    enabled: bool = True


# ---------------------------------------------------------------------------
# Award registry
# ---------------------------------------------------------------------------

AWARD_REGISTRY: list[AwardConfig] = [
    AwardConfig(
        id="case_of_the_mondays",
        name="Case of the Mondays",
        description=(
            "Most Monday games picked correctly (count of games, not points). "
            "Monday Night Football specialist."
        ),
        aggregation=AggregationType.COUNT,
        filters=[
            FilterConfig("is_correct",   FilterOperator.EQ,  True),
            FilterConfig("day_of_week",  FilterOperator.EQ,  "Monday"),
        ],
    ),
    AwardConfig(
        id="the_grand_slam",
        name="The Grand Slam",
        description=(
            "Most complete sets of correctly picked games across all confidence "
            "point values 5–16. A 'set' is defined by the minimum number of "
            "correct picks you have at every single point value in that range. "
            "Example: if you have 8 correct at every value except 11 (only 7), "
            "you have 7 complete sets."
        ),
        aggregation=AggregationType.COMPLETE_SETS,
        filters=[FilterConfig("is_correct", FilterOperator.EQ, True)],
        group_field="confidence_points",
        group_values=list(range(5, 17)),   # 5, 6, 7, …, 16
    ),
    AwardConfig(
        id="the_meticulous",
        name="The Meticulous",
        description=(
            "Most correct picks using a confidence point value of 4 or less. "
            "Rewards players who nail the low-confidence games."
        ),
        aggregation=AggregationType.COUNT,
        filters=[
            FilterConfig("is_correct",         FilterOperator.EQ,  True),
            FilterConfig("confidence_points",   FilterOperator.LTE, 4),
        ],
    ),
    AwardConfig(
        id="nail_biter",
        name="Nail Biter",
        description=(
            "Most correct picks where the covering team won by 3 or fewer points "
            "above the spread. The tighter the margin, the more it counts."
        ),
        aggregation=AggregationType.COUNT,
        filters=[
            FilterConfig("is_correct",    FilterOperator.EQ,  True),
            FilterConfig("cover_margin",  FilterOperator.LTE, 3.0),
        ],
    ),
    AwardConfig(
        id="bottom_feeder",
        name="Bottom Feeder",
        description=(
            "Most confidence points earned from correctly picking teams that did "
            "not make the playoffs. Celebrates finding value where others don't look."
        ),
        aggregation=AggregationType.SUM,
        aggregate_field="confidence_points",
        filters=[
            FilterConfig("is_correct",         FilterOperator.EQ, True),
            FilterConfig("team_made_playoffs",  FilterOperator.EQ, False),
        ],
    ),
    AwardConfig(
        id="the_contrarian",
        name="The Contrarian",
        description=(
            "Most contrarian points across the season. Each game, every player is "
            "placed on a number line: +N if they picked the covering team for N "
            "confidence points, −N if they picked the losing team. A player earns "
            "points equal to their positive distance from every other player. You "
            "do not need to pick the winner to earn contrarian points — being less "
            "wrong than everyone else still pays off."
        ),
        aggregation=AggregationType.CONTRARIAN,
        filters=[],
    ),
]


def get_award(award_id: str) -> AwardConfig | None:
    """Look up an award by ID from the registry."""
    return next((a for a in AWARD_REGISTRY if a.id == award_id), None)


# ---------------------------------------------------------------------------
# Pick context builder
# ---------------------------------------------------------------------------

def _build_pick_contexts(
    db: Session,
    season_id: int,
    playoff_teams: set[str],
) -> list[dict]:
    """
    Return a flat list of pick-context dicts for every finalized pick in the
    season. Each dict exposes raw and derived fields for filter evaluation.

    Derived fields added here:
      day_of_week      – "Monday", "Tuesday", … from game.kickoff_time
      cover_margin     – how many points above the spread the covering team won
                         by (None if not a correct pick or data is missing)
      team_made_playoffs – True if picked_team is in the playoff_teams set
    """
    picks = (
        db.query(Pick)
        .join(Game, Pick.game_id == Game.id)
        .filter(Pick.season_id == season_id, Game.is_final == True)
        .all()
    )

    contexts: list[dict] = []
    for pick in picks:
        game = pick.game

        # cover_margin: abs(home_margin + spread) gives the margin above spread
        # for whichever team covered. Only meaningful for correct picks.
        cover_margin: float | None = None
        if (
            pick.is_correct
            and game.home_score is not None
            and game.away_score is not None
            and game.spread is not None
        ):
            home_margin = game.home_score - game.away_score
            cover_margin = abs(home_margin + game.spread)

        contexts.append({
            # identifiers
            "pick_id":           pick.id,
            "user_id":           pick.user_id,
            "game_id":           pick.game_id,
            "week_id":           pick.week_id,
            "season_id":         pick.season_id,
            # pick fields
            "picked_team":       pick.picked_team,
            "confidence_points": pick.confidence_points,
            "is_correct":        pick.is_correct,
            "points_earned":     pick.points_earned,
            # game fields
            "home_team":         game.home_team,
            "away_team":         game.away_team,
            "home_covered":      game.home_covered,
            "home_score":        game.home_score,
            "away_score":        game.away_score,
            "spread":            game.spread,
            # derived
            "day_of_week":       (
                game.kickoff_time.strftime("%A") if game.kickoff_time else None
            ),
            "cover_margin":      cover_margin,
            "team_made_playoffs": pick.picked_team in playoff_teams,
        })

    return contexts


# ---------------------------------------------------------------------------
# Filter evaluation
# ---------------------------------------------------------------------------

def _apply_filter(ctx: dict, f: FilterConfig) -> bool:
    value = ctx.get(f.field)
    if f.op == FilterOperator.EQ:
        return value == f.value
    if f.op == FilterOperator.LTE:
        return value is not None and value <= f.value
    if f.op == FilterOperator.GTE:
        return value is not None and value >= f.value
    if f.op == FilterOperator.IN:
        return value in f.value
    if f.op == FilterOperator.NOT_IN:
        return value not in f.value
    return False


def _passes_filters(ctx: dict, filters: list[FilterConfig]) -> bool:
    return all(_apply_filter(ctx, f) for f in filters)


# ---------------------------------------------------------------------------
# Aggregation strategies
# ---------------------------------------------------------------------------

def _compute_count_or_sum(
    config: AwardConfig,
    contexts: list[dict],
    player_ids: list[int],
) -> dict[int, float]:
    scores: dict[int, float] = {pid: 0.0 for pid in player_ids}
    for ctx in contexts:
        uid = ctx["user_id"]
        if uid not in scores:
            continue
        if not _passes_filters(ctx, config.filters):
            continue
        if config.aggregation == AggregationType.COUNT:
            scores[uid] += 1.0
        else:  # SUM
            scores[uid] += float(ctx.get(config.aggregate_field) or 0)
    return scores


def _compute_complete_sets(
    config: AwardConfig,
    contexts: list[dict],
    player_ids: list[int],
) -> dict[int, float]:
    """
    For each player, count qualifying picks per expected group_value, then
    take the minimum — that's the number of complete sets.
    """
    assert config.group_field and config.group_values, (
        "COMPLETE_SETS requires group_field and group_values"
    )
    group_value_set = set(config.group_values)

    # user_id → {group_value → count}
    user_counts: dict[int, dict[Any, int]] = {
        pid: {v: 0 for v in config.group_values} for pid in player_ids
    }

    for ctx in contexts:
        uid = ctx["user_id"]
        if uid not in user_counts:
            continue
        if not _passes_filters(ctx, config.filters):
            continue
        val = ctx.get(config.group_field)
        if val in group_value_set:
            user_counts[uid][val] += 1

    return {uid: float(min(counts.values())) for uid, counts in user_counts.items()}


def _compute_contrarian(
    contexts: list[dict],
    player_ids: list[int],
) -> dict[int, float]:
    """
    The Contrarian scoring algorithm.

    Per game:
      - Each player's position = +confidence_points if they picked the covering
        team, −confidence_points if they picked the losing team.
      - Each player earns points = sum of max(0, pos_self − pos_other) for all
        other players who submitted a pick for that game.

    Season total = sum of per-game contrarian points.
    """
    player_id_set = set(player_ids)
    scores: dict[int, float] = {pid: 0.0 for pid in player_ids}

    # Group by game (only finalized games with a known result)
    game_picks: dict[int, list[dict]] = defaultdict(list)
    for ctx in contexts:
        if ctx["home_covered"] is not None and ctx["user_id"] in player_id_set:
            game_picks[ctx["game_id"]].append(ctx)

    for game_id, picks in game_picks.items():
        # Build position for every player who picked this game
        positions: dict[int, int] = {}
        for ctx in picks:
            picked_home = ctx["picked_team"] == ctx["home_team"]
            picked_winner = (picked_home and ctx["home_covered"]) or (
                not picked_home and not ctx["home_covered"]
            )
            sign = 1 if picked_winner else -1
            positions[ctx["user_id"]] = sign * ctx["confidence_points"]

        # Each player earns the sum of positive distances to all others to their left
        player_positions = list(positions.items())
        for uid, pos_i in player_positions:
            earned = sum(
                max(0.0, pos_i - pos_j)
                for uj, pos_j in player_positions
                if uj != uid
            )
            scores[uid] = scores.get(uid, 0.0) + earned

    return scores


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_award(
    config: AwardConfig,
    contexts: list[dict],
    player_ids: list[int],
) -> dict[int, float]:
    """
    Compute scores for one award given pre-built pick contexts.
    Returns {user_id: score} for every player_id.
    """
    if config.aggregation == AggregationType.CONTRARIAN:
        return _compute_contrarian(contexts, player_ids)
    if config.aggregation == AggregationType.COMPLETE_SETS:
        return _compute_complete_sets(config, contexts, player_ids)
    return _compute_count_or_sum(config, contexts, player_ids)


def compute_all_awards(
    db: Session,
    season_id: int,
    registry: list[AwardConfig] | None = None,
) -> dict[str, dict[int, float]]:
    """
    Compute every enabled award for a season.
    Returns {award_id: {user_id: score}}.

    Pass a custom registry to override AWARD_REGISTRY (useful for testing or
    league-specific customisation without editing this file).
    """
    if registry is None:
        registry = AWARD_REGISTRY

    playoff_teams: set[str] = {
        pt.team_abbreviation
        for pt in db.query(PlayoffTeam).filter_by(season_id=season_id).all()
    }

    # All users who made at least one pick this season
    player_ids: list[int] = [
        row[0]
        for row in db.query(Pick.user_id)
        .filter(Pick.season_id == season_id)
        .distinct()
        .all()
    ]

    contexts = _build_pick_contexts(db, season_id, playoff_teams)

    results: dict[str, dict[int, float]] = {}
    for cfg in registry:
        if cfg.enabled:
            results[cfg.id] = compute_award(cfg, contexts, player_ids)
            logger.debug("Computed award '%s' for season %d", cfg.id, season_id)

    return results


def rank_award(
    scores: dict[int, float],
    users_by_id: dict[int, User],
    win_condition: str = "most",
) -> list[dict]:
    """
    Sort {user_id: score} into a ranked list of dicts: {rank, user, score}.
    Ties share the same rank number.
    """
    reverse = win_condition == "most"
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=reverse)

    ranked: list[dict] = []
    prev_score: float | None = None
    prev_rank = 0
    for i, (uid, score) in enumerate(sorted_scores, 1):
        if score != prev_score:
            prev_rank = i
        prev_score = score
        if uid in users_by_id:
            ranked.append({"rank": prev_rank, "user": users_by_id[uid], "score": score})

    return ranked
