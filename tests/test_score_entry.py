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
import pathlib
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


def match_row(game, espn_game_id, home=None, away=None):
    """One feed row, as the live feed sends it: an id plus the matchup."""
    return {
        "espn_game_id": espn_game_id,
        "home_team": home or game.home_team,
        "away_team": away or game.away_team,
        "home_score": None,
        "away_score": None,
        "is_final": False,
        "is_in_progress": False,
        "quarter": None,
        "time_remaining": None,
    }


def test_feed_row_matches_by_matchup_when_the_id_moved():
    """The "spreads populate, scores don't" shape.

    A week imported before nflverse carried ESPN's event ids holds ids the live
    feed never sends, so every score row was dropped while spreads — matched on
    team name — filled in normally. Matching falls back to the matchup and
    writes the feed's id onto the row.
    """
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]
    stale_id = game.espn_game_id

    matched = scheduler.match_feed_row_to_game(db, week, match_row(game, "401872932"))
    assert matched is not None and matched.id == game.id, "feed row must find its game"

    db.commit()
    db.refresh(game)
    assert game.espn_game_id == "401872932", game.espn_game_id
    assert game.espn_game_id != stale_id
    db.close()


def test_matched_row_scores_the_game():
    """End to end: a row whose id does not match still updates the score."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    game = games[0]

    row = match_row(game, "401872933")
    row.update({"home_score": 27, "away_score": 13, "is_final": True})
    matched = scheduler.match_feed_row_to_game(db, week, row)
    assert scheduler.apply_feed_scores(db, matched, row) is True

    reload(db, game)
    assert game.is_final is True and game.home_score == 27, (game.is_final, game.home_score)
    pick = db.query(Pick).filter(Pick.game_id == game.id).first()
    assert pick.is_correct is True, "picks are scored off a matched feed row"
    db.close()


def test_feed_row_for_another_week_is_not_borrowed():
    db = SessionLocal()
    week_a, _, games_a = make_week(db)
    week_b, _, games_b = make_week(db)

    # Week B's feed row carrying week A's game id belongs to neither: the id
    # wins, and it is not this week's game.
    row = match_row(games_b[0], games_a[0].espn_game_id)
    assert scheduler.match_feed_row_to_game(db, week_b, row) is None
    db.close()


def test_unknown_matchup_matches_nothing():
    db = SessionLocal()
    week, _, games = make_week(db)
    row = match_row(games[0], "401999999", home="ZZZ", away="YYY")
    assert scheduler.match_feed_row_to_game(db, week, row) is None
    db.close()


def test_week_with_no_espn_week_still_syncs():
    """A week set up by hand synced spreads but never a single score."""
    from datetime import datetime, timedelta

    db = SessionLocal()
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()
    week = Week(season_id=season.id, week_number=1)   # no espn_week, no first_kickoff
    db.add(week)
    db.flush()
    db.add(Game(
        week_id=week.id,
        espn_game_id=f"kickoff-{week.id}",
        home_team="NE", away_team="SEA",
        kickoff_time=datetime.utcnow() - timedelta(hours=3),
    ))
    db.commit()

    assert [w.week_number for w in scheduler.get_syncable_weeks(db, season)] == [1]
    db.close()


def test_week_whose_games_have_not_kicked_off_is_skipped():
    from datetime import datetime, timedelta

    db = SessionLocal()
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()
    week = Week(season_id=season.id, week_number=1)
    db.add(week)
    db.flush()
    db.add(Game(
        week_id=week.id,
        espn_game_id=f"future-{week.id}",
        home_team="NE", away_team="SEA",
        kickoff_time=datetime.utcnow() + timedelta(days=3),
    ))
    db.commit()

    assert scheduler.get_syncable_weeks(db, season) == []
    db.close()


def test_nflverse_cache_expires():
    """The fallback feed used to freeze on the first snapshot it ever fetched."""
    from datetime import datetime, timedelta, timezone

    from app.services import espn

    espn._nflverse_cache = [{"season": "2026"}]
    espn._nflverse_fetched_at = datetime.now(timezone.utc)
    assert espn._cache_is_fresh() is True

    stale = datetime.now(timezone.utc) + espn.NFLVERSE_CACHE_TTL + timedelta(seconds=1)
    assert espn._cache_is_fresh(now=stale) is False, "a cached CSV must not be served forever"

    espn._nflverse_cache = None
    espn._nflverse_fetched_at = None
    assert espn._cache_is_fresh() is False


def _patch_feed(rows, source="espn", error=None):
    """Swap the live feed for a canned response; returns the undo."""
    from app.services import espn

    original = espn.fetch_live_scores_with_meta

    async def fake(season_year, week):
        return rows, {"source": source, "error": error}

    espn.fetch_live_scores_with_meta = fake
    return lambda: setattr(espn, "fetch_live_scores_with_meta", original)


def test_sync_button_scores_a_week_whose_ids_are_stale():
    """The whole path: button -> feed -> matched by matchup -> picks scored."""
    db = SessionLocal()
    week, contributor, games = make_week(db)
    rows = []
    for i, game in enumerate(games):
        row = match_row(game, f"40188000{i}")
        row.update({"home_score": 24, "away_score": 10, "is_final": True})
        rows.append(row)

    undo = _patch_feed(rows)
    try:
        resp = client_for(contributor).post(
            "/admin/scores/sync", data={"week_id": week.id}, follow_redirects=False
        )
    finally:
        undo()
    assert resp.status_code == 303, (resp.status_code, resp.text[:200])
    from urllib.parse import unquote
    assert "matched 2 of 2" in unquote(resp.headers["location"]), resp.headers["location"]

    reload(db, *games)
    for game in games:
        assert game.is_final is True and game.home_score == 24, (game.is_final, game.home_score)
        assert game.espn_game_id.startswith("40188000"), game.espn_game_id
    assert all(p.is_correct is True for p in db.query(Pick).filter(Pick.week_id == week.id))

    status = scheduler.get_score_sync_status(db)
    assert status["trigger"] == "manual" and status["weeks"][0]["updated"] == 2, status
    db.close()


def test_scores_page_shows_the_last_sync():
    db = SessionLocal()
    week, contributor, games = make_week(db)
    scheduler.record_score_sync_status(db, {
        "ran_at": "2026-09-13T18:05:00",
        "trigger": "scheduled",
        "weeks": [{"week": week.week_number, "source": "nflverse", "games": 2,
                   "matched": 1, "updated": 0, "unmatched": ["A1@H1"], "error": None}],
    })

    resp = client_for(contributor).get(f"/admin/scores?week_id={week.id}")
    assert resp.status_code == 200, resp.status_code
    assert "Last score sync" in resp.text
    assert "matched 1 of 2 games" in resp.text, "the page must say what the feed matched"
    assert "A1@H1" in resp.text, "a game the feed never mentions has to be visible"
    db.close()


def _scoreboard_payload(host_style, event_id="401872656"):
    """A scoreboard reply shaped the way that host shapes it."""
    event = {
        "id": event_id,
        "date": "2026-09-13T17:00Z",
        "status": {"type": {"completed": True, "state": "post"}, "period": 4, "displayClock": "0:00"},
        "competitions": [{"competitors": [
            {"homeAway": "home", "score": "27", "team": {"abbreviation": "SEA", "displayName": "Seattle Seahawks"}},
            {"homeAway": "away", "score": "13", "team": {"abbreviation": "NE", "displayName": "New England Patriots"}},
        ]}],
    }
    if host_style == "cdn":
        return {"content": {"sbData": {"events": [event]}}}
    return {"events": [event]}


def test_espn_is_not_asked_as_a_browser():
    """Measured against the live API: a Chrome User-Agent is what got a 403.

    site.api served bare requests (curl, python-httpx) and refused the same
    request carrying browser headers — a Chrome UA with none of a browser's
    other evidence reads as a bot in a costume. Putting one back reintroduces
    the outage this file exists to prevent.
    """
    from app.services import espn

    for header in ("User-Agent", "Referer", "Origin", "Accept-Language"):
        assert header not in espn.ESPN_HEADERS, f"{header} is what ESPN refuses"
    assert "Accept" in espn.ESPN_HEADERS, espn.ESPN_HEADERS

    source = pathlib.Path(espn.__file__).read_text()
    assert "Mozilla" not in source, "no browser impersonation anywhere in the ESPN client"


def test_cdn_payload_is_read_like_the_api_payload():
    """cdn.espn.com wraps the same scoreboard one layer deeper."""
    from app.services import espn

    for style in ("api", "cdn"):
        events = espn._scoreboard_events(_scoreboard_payload(style))
        assert len(events) == 1, (style, events)
        assert events[0]["id"] == "401872656", (style, events)

    assert espn._scoreboard_events({}) == []
    assert espn._scoreboard_events({"content": {}}) == []
    assert espn._scoreboard_events(None) == []


def test_cdn_takes_its_week_in_its_own_spelling():
    from app.services import espn

    api = espn._scoreboard_params("site.api", 2026, 2)
    assert api["season"] == 2026 and api["week"] == 2, api

    cdn = espn._scoreboard_params("cdn", 2026, 2)
    assert cdn["year"] == 2026 and cdn["week"] == 2 and cdn["xhr"] == 1, cdn


def test_a_403_moves_on_to_the_next_espn_host():
    """The live report: site.api answers 403, so try the others before nflverse."""
    import asyncio

    from app.services import espn

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, replies):
            self.replies = replies
            self.asked = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            self.asked.append(url)
            return self.replies.pop(0)

    client = FakeClient([
        FakeResponse(403),
        FakeResponse(403),
        FakeResponse(200, _scoreboard_payload("cdn")),
    ])
    original = espn.httpx.AsyncClient
    espn.httpx.AsyncClient = lambda *a, **k: client
    try:
        events, endpoint, attempts = asyncio.run(espn.fetch_espn_scoreboard(2026, 1))
    finally:
        espn.httpx.AsyncClient = original

    assert len(events) == 1, events
    assert endpoint == "cdn", endpoint
    assert attempts == ["site.api 403", "web.api 403", "cdn ok"], attempts
    assert len(client.asked) == 3, client.asked


def test_every_host_refusing_names_them_all():
    import asyncio

    from app.services import espn

    class FakeResponse:
        status_code = 403

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return FakeResponse()

    original = espn.httpx.AsyncClient
    espn.httpx.AsyncClient = lambda *a, **k: FakeClient()
    try:
        events, endpoint, attempts = asyncio.run(espn.fetch_espn_scoreboard(2026, 1))
    finally:
        espn.httpx.AsyncClient = original

    assert events == [] and endpoint is None
    assert attempts == ["site.api 403", "web.api 403", "cdn 403"], attempts


def test_sync_summary_names_the_espn_host_that_answered():
    line = scheduler.describe_sync_summary({
        "week": 1, "source": "espn", "endpoint": "cdn", "games": 16,
        "matched": 16, "updated": 4, "unmatched": [], "error": None,
    })
    assert "ESPN (cdn)" in line, line
    assert "matched 16 of 16" in line, line


def test_sync_status_round_trips():
    """The scores page reads this back; a bad write must not hide the sync."""
    db = SessionLocal()
    status = {
        "ran_at": "2026-09-13T18:05:00",
        "trigger": "manual",
        "weeks": [{"week": 2, "source": "espn", "games": 16, "matched": 16, "updated": 3,
                   "unmatched": [], "error": None}],
    }
    scheduler.record_score_sync_status(db, status)

    stored = scheduler.get_score_sync_status(db)
    assert stored["trigger"] == "manual", stored
    assert stored["ran_at"].hour == 18, stored["ran_at"]
    assert stored["weeks"][0]["matched"] == 16, stored["weeks"]
    db.close()


def test_sync_summary_names_what_the_feed_missed():
    line = scheduler.describe_sync_summary({
        "week": 2, "source": None, "games": 16, "matched": 0, "updated": 0,
        "unmatched": ["DET@BUF", "CAR@ATL"], "error": "ESPN unreachable (timeout)",
    })
    assert "matched 0 of 16" in line, line
    assert "DET@BUF" in line, line
    assert "ESPN unreachable" in line, line


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
