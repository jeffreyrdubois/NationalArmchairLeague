"""Tests for the prize payout plan, the playground, and who still needs paying.

Two things about this feature are easy to get wrong and expensive to discover
in January:

1. **Backdating.** Nothing about a payout is snapshotted — raise first place
   from $10 to $15 in week 6 and the player who won week 1 has to be owed $15,
   with nothing to go back and re-key. That only holds if every number on the
   page is recomputed from the plan on every load.
2. **The pool adding up.** A plan that quietly commits $1,040 of a $1,000 pool
   is the whole reason the playground exists, so the arithmetic behind that
   warning is worth pinning down — ties included, because a shared podium has
   to pay out the same total as an outright one.

Run with: python tests/test_payouts.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# A scratch file, never a real database: the routes open their own session, so
# an in-memory database would give them an empty one of their own.
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-payouts-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import (
    Game, PayoutPlan, PayoutRule, Pick, Role, Season, Transaction, User, Week,
)
from app.routers import admin
from app.services import payouts
from app.services.scoring import update_game_results

app = FastAPI()
app.include_router(admin.router)

_next_year = iter(range(2100, 2400))


def make_season(db, n_weeks=2, n_players=4, games_per_week=2):
    """A season whose weeks finish with a clean, separated leaderboard.

    Player i picks the home team on every game for i+1 points, and home always
    covers, so the order is player 4 > player 3 > player 2 > player 1 with no
    accidental ties to reason about.
    """
    # Only one season is ever active in the app, and the funds page works off
    # whichever that is — so the fixture has to keep that true too.
    db.query(Season).update({"is_active": False})
    season = Season(year=next(_next_year), is_active=True)
    db.add(season)
    db.flush()

    admin_user = User(
        first_name="Ada", last_name="Admin",
        email=f"ada{season.id}@example.com", password_hash="x", role=Role.admin,
    )
    db.add(admin_user)
    players = []
    for i in range(n_players):
        player = User(
            first_name=f"Player{i + 1}", last_name=f"Zed{i + 1}",
            email=f"p{i}s{season.id}@example.com", password_hash="x", role=Role.player,
        )
        db.add(player)
        players.append(player)
    db.flush()

    weeks = []
    for w in range(1, n_weeks + 1):
        week = Week(season_id=season.id, week_number=w, espn_week=w, label=f"Week {w}")
        db.add(week)
        db.flush()
        for g in range(games_per_week):
            game = Game(
                week_id=week.id, espn_game_id=f"s{season.id}-w{w}-g{g}",
                home_team=f"H{g}", away_team=f"A{g}", spread=-3.5,
            )
            db.add(game)
            db.flush()
            for i, player in enumerate(players):
                db.add(Pick(
                    user_id=player.id, game_id=game.id, week_id=week.id,
                    season_id=season.id, picked_team=game.home_team,
                    # Distinct points per player so nobody ties by accident.
                    confidence_points=(i + 1) + (g * n_players),
                ))
        weeks.append(week)
    db.commit()
    return season, weeks, players, admin_user


def finish_week(db, week):
    """Play out every game in a week: home wins big, so home covers."""
    for game in db.query(Game).filter(Game.week_id == week.id).all():
        game.home_score, game.away_score, game.is_final = 30, 10, True
        db.flush()
        update_game_results(db, game)
    db.commit()


def client_for(user):
    return TestClient(app, cookies={"access_token": create_access_token(user.id)})


def a_plan(**kw):
    base = dict(
        pool=1000.0, paid_weeks=18,
        weekly={1: 15.0, 2: 10.0, 3: 5.0},
        season={1: 250.0, 2: 120.0, 3: 60.0, 4: 30.0},
        awards={}, is_configured=True,
    )
    base.update(kw)
    return payouts.Plan(**base)


# ---------------------------------------------------------------------------
# The arithmetic the playground runs on
# ---------------------------------------------------------------------------

def test_plan_totals_price_a_full_pool():
    totals = payouts.plan_totals(a_plan())
    assert totals["weekly_per_week"] == 30.0, totals
    assert totals["weekly_total"] == 540.0, totals     # 30 x 18 weeks
    assert totals["season_total"] == 460.0, totals
    assert totals["allocated"] == 1000.0, totals
    assert totals["remaining"] == 0.0, totals
    assert totals["is_balanced"] and not totals["is_over"], totals


def test_plan_totals_flag_an_underspent_pool():
    totals = payouts.plan_totals(a_plan(season={1: 200.0}))
    assert totals["remaining"] == 260.0, totals
    assert not totals["is_balanced"] and not totals["is_over"], totals


def test_plan_totals_flag_an_overspent_pool():
    totals = payouts.plan_totals(a_plan(weekly={1: 20.0, 2: 15.0, 3: 10.0}))
    assert totals["allocated"] == 1270.0, totals       # 45 x 18 + 460
    assert totals["remaining"] == -270.0, totals
    assert totals["is_over"], totals


def test_a_dollar_short_is_not_balanced():
    """Cents matter: 'fully allocated' has to mean exactly."""
    totals = payouts.plan_totals(a_plan(pool=1000.01))
    assert not totals["is_balanced"], totals
    assert totals["remaining"] == 0.01, totals


def test_blank_places_are_not_prizes():
    totals = payouts.plan_totals(a_plan(weekly={1: 15.0, 2: 0.0, 3: 5.0}))
    assert totals["weekly_per_week"] == 20.0, totals


# ---------------------------------------------------------------------------
# Splitting a shared podium
# ---------------------------------------------------------------------------

def test_a_tie_absorbs_the_places_it_spans():
    """Two tied for first share 1st + 2nd; third place still gets 3rd."""
    result = payouts.allocate([[1, 2], [3]], {1: 15.0, 2: 10.0, 3: 5.0})
    assert result == {1: 12.5, 2: 12.5, 3: 5.0}, result


def test_a_tie_pays_out_the_same_total_as_an_outright_win():
    amounts = {1: 15.0, 2: 10.0, 3: 5.0}
    outright = sum(payouts.allocate([[1], [2], [3]], amounts).values())
    three_way = sum(payouts.allocate([[1, 2, 3]], amounts).values())
    assert round(outright, 2) == round(three_way, 2) == 30.0, (outright, three_way)


def test_an_odd_cent_is_never_lost():
    shares = payouts.split_evenly(10.0, 3)
    assert shares == [3.34, 3.33, 3.33], shares
    assert round(sum(shares), 2) == 10.0, shares


def test_a_tie_below_the_money_pays_nobody():
    result = payouts.allocate([[1], [2], [3, 4]], {1: 15.0, 2: 10.0})
    assert result == {1: 15.0, 2: 10.0}, result


# ---------------------------------------------------------------------------
# What a plan actually pays against real results
# ---------------------------------------------------------------------------

def test_a_finished_week_pays_its_top_three():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db)
    finish_week(db, weeks[0])

    report = payouts.compute_payouts(db, season, a_plan())
    assert report.weeks_paid == 1, report.weeks_paid
    lines = report.weeks[0]["lines"]
    assert [l.place for l in lines] == ["1st", "2nd", "3rd"], [l.place for l in lines]
    # Highest confidence points wins, so the last player created is first.
    assert lines[0].user.id == players[3].id, lines[0].user.full_name
    assert [l.amount for l in lines] == [15.0, 10.0, 5.0], [l.amount for l in lines]
    assert report.earned_total == 30.0, report.earned_total
    db.close()


def test_an_unfinished_week_pays_nobody():
    """A week is only in the books once its last game is final."""
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, games_per_week=2)
    games = db.query(Game).filter(Game.week_id == weeks[0].id).all()
    games[0].home_score, games[0].away_score, games[0].is_final = 30, 10, True
    db.commit()
    update_game_results(db, games[0])

    report = payouts.compute_payouts(db, season, a_plan())
    assert report.weeks_paid == 0, report.weeks_paid
    assert report.earned_total == 0.0, report.earned_total
    db.close()


def test_season_and_award_money_stays_a_projection_mid_season():
    """Week 2 of 18 is no time to be told you owe somebody $250."""
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, n_weeks=2)
    finish_week(db, weeks[0])
    finish_week(db, weeks[1])

    report = payouts.compute_payouts(db, season, a_plan())
    assert not report.season_complete, "18 weeks were budgeted; 2 have been played"
    assert report.season_lines, "the standings still have a leader to show"
    assert all(not line.earned for line in report.season_lines)
    # Two weeks at $30 is all that is actually owed.
    assert report.earned_total == 60.0, report.earned_total
    assert report.projected_total == 460.0, report.projected_total
    db.close()


def test_season_money_is_owed_once_the_season_is_done():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, n_weeks=2)
    finish_week(db, weeks[0])
    finish_week(db, weeks[1])

    # A two-week season: the plan only ever budgeted for the weeks that exist.
    report = payouts.compute_payouts(db, season, a_plan(paid_weeks=2))
    assert report.season_complete, "every week that has games has finished"
    assert all(line.earned for line in report.season_lines)
    assert report.projected_total == 0.0, report.projected_total
    assert report.earned_total == 60.0 + 460.0, report.earned_total
    db.close()


def test_an_unconfigured_season_pays_nobody():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db)
    finish_week(db, weeks[0])

    report = payouts.compute_payouts(db, season, payouts.load_plan(db, season.id))
    assert report.lines == [], report.lines
    db.close()


def test_weeks_outside_the_paid_range_do_not_pay():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, n_weeks=3)
    for week in weeks:
        finish_week(db, week)

    report = payouts.compute_payouts(db, season, a_plan(paid_weeks=2))
    assert report.weeks_paid == 2, report.weeks_paid
    assert {w["week"].week_number for w in report.weeks} == {1, 2}
    db.close()


def test_an_award_nobody_has_scored_on_pays_nothing():
    """Bottom Feeder sits at zero until eliminations start.

    A table of zeroes still sorts, so without this the prize would go to
    whoever happened to come out on top of it.
    """
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, n_weeks=1)
    finish_week(db, weeks[0])

    plan = a_plan(paid_weeks=1, weekly={}, season={}, awards={"bottom_feeder": 100.0})
    report = payouts.compute_payouts(db, season, plan)
    assert report.season_complete, "the one week that exists has been played"
    assert report.award_lines[0]["lines"] == [], report.award_lines[0]["lines"]
    assert report.earned_total == 0.0, report.earned_total
    db.close()


def test_a_tied_award_splits_its_prize():
    """Everyone has the same one low-confidence correct pick, so all four share."""
    db = SessionLocal()
    season, weeks, players, _ = make_season(db, n_weeks=1)
    finish_week(db, weeks[0])

    plan = a_plan(paid_weeks=1, weekly={}, season={}, awards={"the_meticulous": 100.0})
    report = payouts.compute_payouts(db, season, plan)
    lines = report.award_lines[0]["lines"]
    assert len(lines) == 4, [l.user.full_name for l in lines]
    assert all(l.amount == 25.0 and l.place == "T-1st" for l in lines), lines
    assert report.earned_total == 100.0, report.earned_total
    db.close()


# ---------------------------------------------------------------------------
# Backdating — the reason nothing is snapshotted
# ---------------------------------------------------------------------------

def test_raising_first_place_repays_a_week_already_played():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])

    payouts.save_plan(db, season, a_plan(weekly={1: 10.0}), admin_user)
    before = payouts.compute_payouts(db, season, payouts.load_plan(db, season.id))
    assert before.earned_total == 10.0, before.earned_total

    # Mid-season change of heart: first place is worth $15 after all.
    payouts.save_plan(db, season, a_plan(weekly={1: 15.0}), admin_user)
    after = payouts.compute_payouts(db, season, payouts.load_plan(db, season.id))
    assert after.earned_total == 15.0, after.earned_total
    assert after.weeks[0]["lines"][0].user.id == players[3].id
    db.close()


def test_dropping_a_place_stops_it_paying_retroactively():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])

    payouts.save_plan(db, season, a_plan(weekly={1: 15.0, 2: 10.0, 3: 5.0}), admin_user)
    assert payouts.compute_payouts(
        db, season, payouts.load_plan(db, season.id)
    ).earned_total == 30.0

    # Third place is cut. The old rule must not survive as a stale row.
    payouts.save_plan(db, season, a_plan(weekly={1: 15.0, 2: 10.0}), admin_user)
    plan = payouts.load_plan(db, season.id)
    assert plan.weekly == {1: 15.0, 2: 10.0}, plan.weekly
    assert payouts.compute_payouts(db, season, plan).earned_total == 25.0
    db.close()


def test_saving_a_plan_replaces_rather_than_accumulates():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    payouts.save_plan(db, season, a_plan(), admin_user)
    payouts.save_plan(db, season, a_plan(weekly={1: 20.0}), admin_user)

    assert db.query(PayoutPlan).filter(PayoutPlan.season_id == season.id).count() == 1
    row = db.query(PayoutPlan).filter(PayoutPlan.season_id == season.id).first()
    weekly = db.query(PayoutRule).filter(
        PayoutRule.plan_id == row.id, PayoutRule.category == payouts.WEEKLY
    ).all()
    assert len(weekly) == 1 and weekly[0].amount == 20.0, weekly
    db.close()


# ---------------------------------------------------------------------------
# Earned vs. logged — the "who do I still owe" table
# ---------------------------------------------------------------------------

def test_the_ledger_subtracts_what_has_already_been_logged():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])
    report = payouts.compute_payouts(db, season, a_plan())

    winner = players[3]
    db.add(Transaction(
        user_id=winner.id, amount=15.0, direction="out",
        note="paid", logged_by_id=admin_user.id,
    ))
    db.commit()

    ledger = {r["user"].id: r for r in payouts.payout_ledger(db, report, players)}
    assert ledger[winner.id]["owed"] == 0.0, ledger[winner.id]
    assert ledger[players[2].id]["owed"] == 10.0, ledger[players[2].id]
    # Entry fees coming *in* are a different column and must not clear a payout.
    db.add(Transaction(
        user_id=players[2].id, amount=50.0, direction="in",
        note="entry fee", logged_by_id=admin_user.id,
    ))
    db.commit()
    ledger = {r["user"].id: r for r in payouts.payout_ledger(db, report, players)}
    assert ledger[players[2].id]["owed"] == 10.0, ledger[players[2].id]
    db.close()


def test_the_ledger_leads_with_whoever_is_owed_most():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db)
    finish_week(db, weeks[0])
    report = payouts.compute_payouts(db, season, a_plan())

    ledger = payouts.payout_ledger(db, report, players)
    assert [r["owed"] for r in ledger] == [15.0, 10.0, 5.0, 0.0], [r["owed"] for r in ledger]
    db.close()


# ---------------------------------------------------------------------------
# The pages themselves
# ---------------------------------------------------------------------------

def test_the_playground_is_admins_only():
    db = SessionLocal()
    season, weeks, players, _ = make_season(db)
    resp = client_for(players[0]).get("/admin/payouts", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/", resp.status_code

    saved = client_for(players[0]).post("/admin/payouts/save", data={
        "season_id": season.id, "pool_amount": "1000", "paid_weeks": "18",
    }, follow_redirects=False)
    assert saved.status_code == 403, saved.status_code
    assert db.query(PayoutPlan).filter(PayoutPlan.season_id == season.id).count() == 0
    db.close()


def _form(season, weekly=(15.0, 10.0, 5.0), pool="1000", weeks="18"):
    """The playground form as the browser posts it — one field per place row."""
    return {
        "season_id": str(season.id),
        "pool_amount": pool,
        "paid_weeks": weeks,
        "weekly_place": [str(i + 1) for i in range(len(weekly))],
        "weekly_amount": [str(a) for a in weekly],
        "season_place": ["1"],
        "season_amount": ["250"],
        "notes": "from the test",
    }


def test_saving_from_the_form_stores_the_plan():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    resp = client_for(admin_user).post(
        "/admin/payouts/save", data=_form(season), follow_redirects=False
    )
    assert resp.status_code == 303, resp.status_code

    plan = payouts.load_plan(db, season.id)
    assert plan.is_configured and plan.pool == 1000.0, plan
    assert plan.weekly == {1: 15.0, 2: 10.0, 3: 5.0}, plan.weekly
    assert plan.season == {1: 250.0}, plan.season
    assert plan.notes == "from the test", plan.notes
    db.close()


def test_a_blank_amount_in_the_form_is_just_not_a_prize():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    client_for(admin_user).post(
        "/admin/payouts/save", data=_form(season, weekly=(15.0, "", 5.0)),
        follow_redirects=False,
    )
    plan = payouts.load_plan(db, season.id)
    assert plan.weekly == {1: 15.0, 3: 5.0}, plan.weekly
    db.close()


def test_preview_prices_the_numbers_without_saving_them():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])

    resp = client_for(admin_user).post(
        "/admin/payouts/preview", data=_form(season, weekly=(99.0,))
    )
    assert resp.status_code == 200, resp.status_code
    assert "Preview" in resp.text
    # The week's winner is shown at the previewed amount...
    assert "$99.00" in resp.text, "preview should price the submitted numbers"
    # ...but nothing was written down.
    assert db.query(PayoutPlan).filter(PayoutPlan.season_id == season.id).count() == 0
    db.close()


def test_the_funds_page_says_who_is_waiting_on_money():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])
    payouts.save_plan(db, season, a_plan(), admin_user)

    resp = client_for(admin_user).get("/admin/funds")
    assert resp.status_code == 200, resp.status_code
    assert "Payouts To Make" in resp.text
    assert players[3].full_name in resp.text
    assert "Log all 3 payouts" in resp.text, "three winners, three payments to make"
    db.close()


def test_logging_all_payouts_clears_the_board():
    db = SessionLocal()
    season, weeks, players, admin_user = make_season(db)
    finish_week(db, weeks[0])
    payouts.save_plan(db, season, a_plan(), admin_user)

    resp = client_for(admin_user).post("/admin/funds/payouts/settle", follow_redirects=False)
    assert resp.status_code == 303, resp.status_code

    logged = db.query(Transaction).filter(Transaction.direction == "out").all()
    assert sorted(t.amount for t in logged) == [5.0, 10.0, 15.0], [t.amount for t in logged]

    report = payouts.compute_payouts(db, season, payouts.load_plan(db, season.id))
    ledger = payouts.payout_ledger(db, report, players)
    assert all(row["owed"] == 0 for row in ledger), ledger

    # Running it again has nothing left to do.
    client_for(admin_user).post("/admin/funds/payouts/settle", follow_redirects=False)
    assert db.query(Transaction).filter(Transaction.direction == "out").count() == 3
    db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
