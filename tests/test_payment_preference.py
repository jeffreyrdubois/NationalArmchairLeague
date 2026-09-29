"""How a player wants prize money sent.

Zelle, Venmo, or Cash App — chosen on Account Settings, shown next to whoever
the league still owes, and settable from League Funds when the commissioner
already knows the answer. A value outside those three is not a preference to
store.

Run with: python tests/test_payment_preference.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-paypref-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, _migrate, engine
from app.models import Role, Season, Transaction, User
from app.routers import admin, dashboard
from app.services import payouts
from app.utils import normalize_payment_method

app = FastAPI()
app.include_router(admin.router)
app.include_router(dashboard.router)


def client_for(user):
    return TestClient(app, cookies={"access_token": create_access_token(user.id)})


def test_only_the_three_rails_are_a_preference():
    assert normalize_payment_method("Zelle") == "zelle"
    assert normalize_payment_method("  Venmo ") == "venmo"
    assert normalize_payment_method("cashapp") == "cashapp"
    assert normalize_payment_method("") is None
    assert normalize_payment_method(None) is None
    try:
        normalize_payment_method("paypal")
    except ValueError:
        pass
    else:
        raise AssertionError("PayPal is the commissioner's handle, not a player preference")


def test_migrate_adds_the_column_to_an_existing_database():
    """create_all on a fresh install has the column; an old file does not."""
    reset_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE users DROP COLUMN preferred_payment")
    _migrate()
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert "preferred_payment" in cols


def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _people(db):
    db.query(Season).update({"is_active": False})
    season = Season(year=2099, is_active=True)
    admin_user = User(
        first_name="Ada", last_name="Admin", email="ada-pay@example.com",
        password_hash="x", role=Role.admin,
    )
    stephen = User(
        first_name="Stephen", last_name="Chandler", email="sc-pay@example.com",
        password_hash="x", role=Role.player,
    )
    other = User(
        first_name="Stephen", last_name="Sutphin", email="ss-pay@example.com",
        password_hash="x", role=Role.player,
    )
    db.add_all([season, admin_user, stephen, other])
    db.commit()
    return season, admin_user, stephen, other


def test_a_player_picks_on_the_settings_page():
    reset_db()
    db = SessionLocal()
    _, _, stephen, _ = _people(db)

    page = client_for(stephen).get("/settings")
    assert page.status_code == 200, page.status_code
    assert "How you want to be paid" in page.text

    resp = client_for(stephen).post(
        "/settings/payment", data={"preferred_payment": "venmo"}, follow_redirects=False,
    )
    assert resp.status_code == 303, resp.status_code
    db.refresh(stephen)
    assert stephen.preferred_payment == "venmo", stephen.preferred_payment

    bad = client_for(stephen).post(
        "/settings/payment", data={"preferred_payment": "bitcoin"}, follow_redirects=False,
    )
    assert bad.status_code == 303 and "error=" in bad.headers["location"]
    db.refresh(stephen)
    assert stephen.preferred_payment == "venmo", "a bad value must not overwrite the saved one"
    db.close()


def test_the_funds_page_shows_it_and_an_admin_can_change_it():
    reset_db()
    db = SessionLocal()
    season, admin_user, stephen, other = _people(db)
    stephen.preferred_payment = "cashapp"
    payouts.save_plan(
        db, season,
        payouts.Plan(pool=100, paid_weeks=18, weekly={1: 15.0}, is_configured=True),
        admin_user,
    )
    # A logged payout puts him on Payouts To Make even before a week is scored,
    # which is the list the preference sits next to.
    db.add(Transaction(
        user_id=stephen.id, amount=15, direction="out", logged_by_id=admin_user.id,
    ))
    db.commit()

    html = client_for(admin_user).get("/admin/funds").text
    assert "Payouts To Make" in html
    # The owed/paid row and the full-roster row both carry his current choice.
    assert html.count('value="cashapp" selected') >= 2, html[html.find("Chandler") - 100:html.find("Chandler") + 500]

    resp = client_for(admin_user).post(
        "/admin/funds/payment",
        data={"user_id": other.id, "preferred_payment": "zelle"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.status_code
    db.refresh(other)
    assert other.preferred_payment == "zelle", other.preferred_payment

    cleared = client_for(admin_user).post(
        "/admin/funds/payment",
        data={"user_id": other.id, "preferred_payment": ""},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    db.refresh(other)
    assert other.preferred_payment is None

    # A player cannot set someone else's, including by posting the admin form.
    forbidden = client_for(stephen).post(
        "/admin/funds/payment",
        data={"user_id": other.id, "preferred_payment": "venmo"},
        follow_redirects=False,
    )
    assert forbidden.status_code == 403, forbidden.status_code
    db.refresh(other)
    assert other.preferred_payment is None
    db.close()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
