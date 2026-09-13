"""Regression tests for entering scores and keeping results consistent.

Two failures put the pick grid into the state Robert reported: finished games
showing as "Upcoming" with red and green picks beside them, and hand-entered
finals that would not stick.

1. The score sync overwrote every game with whatever the feed said. When ESPN
   was unreachable the nflverse fallback returns the week with empty scores, so
   five minutes after a contributor saved a final it was blanked back to
   not-final — while the picks kept the results they had already been given.
2. Blank score boxes posted as empty strings against an ``int | None`` form
   field, which FastAPI rejected outright (422), so a half-filled save looked
   like the app refusing to save at all.

Run with: python tests/test_score_entry.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# A scratch file, never a real database: the routes open their own session, so
# an in-memory database would give them an empty one of their own.
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-scores-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Game, Pick, Role, ScoreSource, Season, User, Week
from app.routers import admin
from app.services import scheduler
from app.services.scoring import repair_pick_scoring

app = FastAPI()
app.include_router(admin.router)

_next_year = iter(range(2026, 2100))


def make_week(db, n_games=2):
    """A season/week/contributor/games fixture with one pick per game."""
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()
    week = Week(season_id=season.id, week_number=1, espn_week=1)
    db.add(week)
    db.flush()
    contributor = User(
        first_name="Robert",
        last_name="Contributor",
        email=f"robert{week.id}@example.com",
        password_hash="x",
        role=Role.contributor,
    )
    db.add(contributor)
    db.flush()

    games = []
    for i in range(n_games):
        game = Game(
            week_id=week.id,
            espn_game_id=f"evt-{week.id}-{i}",
            home_team=f"H{i}",
            away_team=f"A{i}",
            spread=-3.5,
        )
        db.add(game)
        db.flush()
        db.add(Pick(
            user_id=contributor.id,
            game_id=game.id,
            week_id=week.id,
            season_id=season.id,
            picked_team=game.home_team,
            confidence_points=i + 1,
        ))
        games.append(game)
    db.commit()
    return week, contributor, games


def client_for(user):
    return TestClient(app, cookies={"access_token": create_access_token(user.id)})


def reload(db, *objs):
    """Re-read rows a request handler wrote from its own session."""
    db.commit()   # end this session's read transaction first
    for obj in objs:
        db.refresh(obj)


def feed_row(game, home_score=None, away_score=None, is_final=False, is_in_progress=False):
    """One row shaped like espn.fetch_live_scores() returns."""
    return {
        "espn_game_id": game.espn_game_id,
        "home_score": home_score,
        "away_score": away_score,
        "is_final": is_final,
        "is_in_progress": is_in_progress,
        "quarter": None,
        "time_remaining": None,
    }


def save_score(client, game, away, home, final=True):
    return client.post("/admin/scores/update", data={
        "game_id": game.id,
        "away_score": away,
        "home_score": home,
        **({"is_final": "true"} if final else {}),
    }, follow_redirects=False)


def test_manual_final_scores_picks():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    resp = save_score(client_for(contributor), game, away=10, home=20)
    assert resp.status_code == 303, resp.status_code

    reload(db, game)
    assert game.is_final is True and game.home_score == 20, (game.is_final, game.home_score)
    assert game.home_covered is True, game.home_covered   # home by 10 vs -3.5
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert pick.is_correct is True and pick.points_earned == 1.0, (pick.is_correct, pick.points_earned)
    db.close()


def test_blank_score_box_is_not_a_422():
    """Half a score is a legitimate save-in-progress, not a rejected request."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    resp = save_score(client_for(contributor), game, away=21, home="", final=True)
    assert resp.status_code == 303, (resp.status_code, resp.text[:200])

    reload(db, game)
    assert game.away_score == 21, game.away_score
    assert game.home_score is None, game.home_score
    assert game.is_final is False, "cannot be final without both scores"
    db.close()


def test_empty_feed_never_wipes_a_manual_final():
    """The exact report: saved final, then the sync ran with nothing to say."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]
    save_score(client_for(contributor), game, away=10, home=20)
    reload(db, game)
    assert game.score_source == ScoreSource.manual, game.score_source

    # nflverse fallback for a week it hasn't published yet: no scores, not final
    scheduler.apply_feed_scores(db, game, feed_row(game))
    reload(db, game)

    assert game.is_final is True, "a saved final must survive the score sync"
    assert (game.away_score, game.home_score) == (10, 20), (game.away_score, game.home_score)
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert pick.is_correct is True, "picks stay scored while their game is final"
    db.close()


def test_empty_feed_never_wipes_a_synced_final():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    scheduler.apply_feed_scores(db, game, feed_row(game, home_score=20, away_score=10, is_final=True))
    reload(db, game)
    assert game.is_final is True and game.home_covered is True

    scheduler.apply_feed_scores(db, game, feed_row(game))          # feed went quiet
    scheduler.apply_feed_scores(db, game, feed_row(game, home_score=0, away_score=0))  # and then dumb
    reload(db, game)

    assert game.is_final is True, "a final game does not come back to life"
    assert (game.away_score, game.home_score) == (10, 20), (game.away_score, game.home_score)
    db.close()


def test_provisional_score_still_takes_live_updates():
    """Typed in mid-game without Final, so the live feed may still refine it."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    save_score(client_for(contributor), game, away=3, home=7, final=False)
    reload(db, game)
    assert game.is_final is False and game.score_source == ScoreSource.api, game.score_source

    scheduler.apply_feed_scores(db, game, feed_row(game, home_score=31, away_score=3, is_final=True))
    reload(db, game)
    assert game.is_final is True and game.home_score == 31, (game.is_final, game.home_score)
    db.close()


def test_feed_still_fills_in_a_game_it_knows_about():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    scheduler.apply_feed_scores(db, game, feed_row(game, home_score=7, away_score=3, is_in_progress=True))
    reload(db, game)
    assert game.is_in_progress is True and game.home_score == 7

    scheduler.apply_feed_scores(db, game, feed_row(game, home_score=31, away_score=3, is_final=True))
    reload(db, game)
    assert game.is_final is True and game.is_in_progress is False
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert pick.is_correct is True, pick.is_correct
    db.close()


def test_correcting_a_final_score_rescores_picks():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]
    client = client_for(contributor)

    save_score(client, game, away=10, home=20)          # home covers -3.5
    reload(db, game)
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert pick.is_correct is True

    save_score(client, game, away=21, home=20)          # typo fixed: away wins
    reload(db, game)
    reload(db, pick)
    assert game.home_covered is False, game.home_covered
    assert pick.is_correct is False and pick.points_earned == 0.0, (pick.is_correct, pick.points_earned)
    db.close()


def test_unfinaling_a_game_resets_its_picks():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]
    client = client_for(contributor)
    save_score(client, game, away=10, home=20)

    resp = client.post("/admin/scores/clear", data={"game_id": game.id}, follow_redirects=False)
    assert resp.status_code == 303, resp.status_code

    reload(db, game)
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert game.is_final is False and game.home_covered is None
    assert pick.is_correct is None and pick.points_earned is None, (pick.is_correct, pick.points_earned)
    # cleared by hand hands the game back to the feed
    assert game.score_source == ScoreSource.api, game.score_source
    db.close()


def test_repair_clears_results_stranded_on_unfinal_games():
    """The damage already in the database: scored picks, no final game."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    stranded, untouched = games[0], games[1]

    save_score(client_for(contributor), stranded, away=10, home=20)
    save_score(client_for(contributor), untouched, away=10, home=20)
    reload(db, stranded)

    # Reproduce the old sync's clobber: score blanked, picks left scored.
    stranded.home_score = None
    stranded.away_score = None
    stranded.is_final = False
    db.commit()

    assert repair_pick_scoring(db) == 1

    stranded_pick = db.query(Pick).filter(Pick.game_id == stranded.id).first()
    assert stranded_pick.is_correct is None, "an unfinished game has no results"
    assert stranded.home_covered is None
    kept = db.query(Pick).filter(Pick.game_id == untouched.id).first()
    assert kept.is_correct is True, "a genuinely final game keeps its results"
    db.close()


def test_sync_covers_every_started_week_not_just_the_first():
    """A week stuck open must not stop later weeks from syncing."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()
    now = datetime.utcnow()
    weeks = []
    for n, kickoff in ((1, now - timedelta(days=8)), (2, now - timedelta(days=1)), (3, now + timedelta(days=6))):
        week = Week(season_id=season.id, week_number=n, espn_week=n, first_kickoff=kickoff)
        db.add(week)
        weeks.append(week)
    db.commit()

    syncable = [w.week_number for w in scheduler.get_syncable_weeks(db, season)]
    assert syncable == [1, 2], syncable   # week 3 hasn't kicked off yet

    weeks[0].is_completed = True
    db.commit()
    assert [w.week_number for w in scheduler.get_syncable_weeks(db, season)] == [2]
    db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
