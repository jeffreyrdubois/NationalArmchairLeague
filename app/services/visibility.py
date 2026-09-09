"""Who may see whose picks, and when.

The pool only works if nobody can read another player's picks while there is
still time to change their own, so the rule lives here rather than being
re-derived in each template: a week's picks stay hidden until that week locks,
which happens at the first kickoff.

Admins are deliberately not an exception. The commissioner plays in the pool
too, so an early peek from an admin account is exactly the advantage the lock
exists to prevent. Entering picks on someone's behalf still lives in the admin
panel, where it is a deliberate action and audit-logged.

The one thing everyone can see before the lock is *who has submitted*, so
players can chase each other for missing picks without learning anything about
the picks themselves.
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Game, Pick, User, Week


def picks_are_revealed(week: Week) -> bool:
    """True once a week's picks may be shown to the whole league."""
    return bool(week and week.is_picks_locked)


def can_see_picks(week: Week, viewer: User, owner_id: int) -> bool:
    """Whether ``viewer`` may see the picks ``owner_id`` made for ``week``."""
    return picks_are_revealed(week) or (viewer is not None and viewer.id == owner_id)


def get_submission_status(db: Session, week: Week) -> list[dict]:
    """Per-player submission progress for a week — counts only, never content.

    Returns one row per active player: ``user``, how many picks they have in
    (``picks_made``) out of ``n_games``, and whether that is the full slate.
    Players who are done sort first, so the outstanding ones are easy to pick
    out at the bottom of the list.
    """
    if not week:
        return []

    n_games = db.query(Game).filter(Game.week_id == week.id).count()
    counts = dict(
        db.query(Pick.user_id, func.count(Pick.id))
        .filter(Pick.week_id == week.id)
        .group_by(Pick.user_id)
        .all()
    )

    rows = []
    for user in db.query(User).filter(User.is_active == True).all():
        made = counts.get(user.id, 0)
        rows.append({
            "user": user,
            "picks_made": made,
            "n_games": n_games,
            "is_complete": n_games > 0 and made >= n_games,
            "has_started": made > 0,
        })

    rows.sort(
        key=lambda r: (
            not r["is_complete"],
            not r["has_started"],
            r["user"].first_name.lower(),
            r["user"].last_name.lower(),
        )
    )
    return rows
