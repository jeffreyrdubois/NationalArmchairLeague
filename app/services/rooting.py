"""Which side of each unfinished game helps a player.

Your pick is not always the side that helps you: if you took a team for 3
points and the people you are racing took it for 14, their cover hurts you.
For every game still to be decided this weighs both outcomes by what each
player gains, against the players you are actually racing:

- week: everyone whose race with you for the week is still live (either of
  you can still finish ahead on points remaining);
- season: the nearest player(s) ahead of and behind you in the standings.

Shared by the all-picks page's "Root For" column and the MCP rooting guide,
so the site and Claude never give different advice.
"""
from sqlalchemy.orm import Session

from app.models import Game, Pick, Week
from app.services import scoring

EITHER = "either"


def outcome_net(deltas: dict[int, float], me: int, rivals: list[int]) -> float:
    """Your gain minus each rival's, summed, for one outcome."""
    return sum(deltas.get(me, 0) - deltas.get(r, 0) for r in rivals)


def pick_side(nets: dict[str, float]) -> str:
    (a, na), (b, nb) = nets.items()
    if na == nb:
        return EITHER
    return a if na > nb else b


def rooting_guide(db: Session, week: Week, me_id: int) -> dict | None:
    """The rooting picture for ``me_id`` in ``week``, or None if they made no
    picks that week.

    Returns the week and season standings rows it was built from, the rivals
    in each race, and per unfinished game (keyed by game id): each outcome's
    per-player gains, the net against each race, and the side to root for.
    """
    week_rows = {
        r["user"].id: r for r in scoring.get_week_standings(db, week.id) if r["user"]
    }
    if me_id not in week_rows:
        return None
    season_rows = [
        r for r in scoring.get_season_standings(db, week.season_id) if r["user"]
    ]

    # --- the week race: who can still finish either side of you ---
    mine = week_rows[me_id]
    week_rivals = [
        uid for uid, r in week_rows.items()
        if uid != me_id
        and r["potential"] >= mine["total"]
        and mine["potential"] >= r["total"]
    ]

    # --- the season race: your nearest neighbours in the standings ---
    season_total = {r["user"].id: r["total"] for r in season_rows}
    my_season = season_total.get(me_id, 0)
    ahead = [t for uid, t in season_total.items() if uid != me_id and t >= my_season]
    behind = [t for uid, t in season_total.items() if uid != me_id and t < my_season]
    season_rivals = [
        uid for uid, t in season_total.items()
        if uid != me_id and (
            (ahead and t == min(ahead)) or (behind and t == max(behind))
        )
    ]

    picks_by_game: dict[int, dict[int, Pick]] = {}
    for pick in db.query(Pick).filter(Pick.week_id == week.id):
        picks_by_game.setdefault(pick.game_id, {})[pick.user_id] = pick

    games = {}
    for game in db.query(Game).filter(Game.week_id == week.id):
        if game.is_final:
            continue
        picks = picks_by_game.get(game.id, {})
        deltas_by_team = {
            team: {
                uid: float(p.confidence_points) if p.picked_team == team else 0.0
                for uid, p in picks.items()
            }
            for team in (game.away_team, game.home_team)
        }
        week_net = {
            t: outcome_net(d, me_id, week_rivals) for t, d in deltas_by_team.items()
        }
        season_net = {
            t: outcome_net(d, me_id, season_rivals) for t, d in deltas_by_team.items()
        }
        your = picks.get(me_id)
        games[game.id] = {
            "your_pick": your,
            "deltas_by_team": deltas_by_team,
            "week_net": week_net,
            "season_net": season_net,
            "root_for_week": (
                pick_side(week_net) if week_rivals
                else (your.picked_team if your else EITHER)
            ),
            "root_for_season": pick_side(season_net) if season_rivals else EITHER,
        }

    return {
        "mine": mine,
        "my_season": my_season,
        "week_rows": week_rows,
        "season_rows": season_rows,
        "season_total": season_total,
        "week_rivals": week_rivals,
        "season_rivals": season_rivals,
        "games": games,
    }


def root_for(guide: dict | None, game: Game) -> str | None:
    """The single side to show on the all-picks page for one game.

    The week race decides while it is live; once nobody can catch you (or you
    can't catch anybody) the season race does. None for a finished game or a
    player with no picks.
    """
    if not guide:
        return None
    row = guide["games"].get(game.id)
    if not row:
        return None
    if guide["week_rivals"]:
        return row["root_for_week"]
    if guide["season_rivals"]:
        return row["root_for_season"]
    your = row["your_pick"]
    return your.picked_team if your else EITHER
