"""Regression tests for editing picks after they've been submitted.

Confidence points are unique per user per week in the database, so the
interesting cases are the ones where a player reshuffles values that are
already in use — swapping two games, or reversing the whole slate. Those
used to fail with an IntegrityError (a 500 in the browser) and left the
player unable to change picks they had already saved.

Run with: python tests/test_pick_editing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# Always in-memory: never touch a real database, whatever DATABASE_URL says.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi import HTTPException

from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Season, User, Week
from app.routers.picks import apply_picks


_next_year = iter(range(2026, 2100))


def make_week(db, n_games=4):
    """A season/week/user/games fixture; each call gets its own season year."""
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()
    week = Week(season_id=season.id, week_number=1)
    db.add(week)
    db.flush()
    user = User(
        first_name="Test",
        last_name="Player",
        email=f"player{week.id}@example.com",
        password_hash="x",
    )
    db.add(user)
    db.flush()
    games = [
        Game(week_id=week.id, home_team=f"H{i}", away_team=f"A{i}")
        for i in range(n_games)
    ]
    db.add_all(games)
    db.commit()
    return week, user, games


def points_by_game(db, user, week):
    return {
        p.game_id: (p.confidence_points, p.picked_team)
        for p in db.query(Pick).filter(
            Pick.user_id == user.id, Pick.week_id == week.id
        )
    }


def test_swap_two_confidence_values():
    db = SessionLocal()
    week, user, games = make_week(db)
    apply_picks(db, user.id, week, {
        games[0].id: (16, "H0"),
        games[1].id: (15, "H1"),
        games[2].id: (14, "H2"),
        games[3].id: (13, "H3"),
    })
    db.commit()

    apply_picks(db, user.id, week, {
        games[0].id: (15, "H0"),
        games[1].id: (16, "H1"),
        games[2].id: (14, "H2"),
        games[3].id: (13, "H3"),
    })
    db.commit()

    saved = points_by_game(db, user, week)
    assert saved[games[0].id][0] == 15, saved
    assert saved[games[1].id][0] == 16, saved
    db.close()


def test_reverse_the_whole_slate():
    db = SessionLocal()
    week, user, games = make_week(db)
    apply_picks(db, user.id, week, {
        g.id: (16 - i, g.home_team) for i, g in enumerate(games)
    })
    db.commit()

    apply_picks(db, user.id, week, {
        g.id: (13 + i, g.away_team) for i, g in enumerate(games)
    })
    db.commit()

    saved = points_by_game(db, user, week)
    assert [saved[g.id][0] for g in games] == [13, 14, 15, 16], saved
    assert [saved[g.id][1] for g in games] == [g.away_team for g in games], saved
    db.close()


def test_edit_clears_previous_scoring():
    db = SessionLocal()
    week, user, games = make_week(db)
    apply_picks(db, user.id, week, {g.id: (16 - i, g.home_team) for i, g in enumerate(games)})
    db.commit()

    scored = db.query(Pick).filter(Pick.game_id == games[0].id).first()
    scored.is_correct = True
    scored.points_earned = 16.0
    db.commit()

    apply_picks(db, user.id, week, {g.id: (16 - i, g.away_team) for i, g in enumerate(games)})
    db.commit()

    db.refresh(scored)
    assert scored.picked_team == games[0].away_team
    assert scored.is_correct is None
    assert scored.points_earned is None
    db.close()


def test_duplicate_points_rejected_cleanly():
    db = SessionLocal()
    week, user, games = make_week(db)
    apply_picks(db, user.id, week, {g.id: (16 - i, g.home_team) for i, g in enumerate(games)})
    db.commit()

    try:
        apply_picks(db, user.id, week, {
            games[0].id: (16, "H0"),
            games[1].id: (16, "H1"),
            games[2].id: (14, "H2"),
            games[3].id: (13, "H3"),
        })
    except HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    else:
        raise AssertionError("duplicate point values should be rejected")
    db.close()


def test_partial_edit_leaves_other_picks_alone():
    """An admin editing one game shouldn't disturb the rest of the slate."""
    db = SessionLocal()
    week, user, games = make_week(db)
    apply_picks(db, user.id, week, {g.id: (16 - i, g.home_team) for i, g in enumerate(games)})
    db.commit()

    apply_picks(db, user.id, week, {games[0].id: (16, games[0].away_team)})
    db.commit()

    saved = points_by_game(db, user, week)
    assert saved[games[0].id] == (16, games[0].away_team), saved
    assert [saved[g.id][0] for g in games] == [16, 15, 14, 13], saved
    db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
