"""The pre-kickoff webhook is a setting, fires once, and is signed.

A hardcoded hour only works when the first game is Thursday night. The lead
time is chosen on the AI settings page, and the POST has to be verifiable so
a forged call can be rejected.

Run with: python tests/test_picks_webhook.py
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-webhook-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Role, Season, User, Week
from app.routers import dashboard
from app.services import picks_webhook

app = FastAPI()
app.include_router(dashboard.router)


def fresh():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    admin = User(first_name="Ada", last_name="Admin", email="admin@x.com",
                 password_hash="x", role=Role.admin, is_active=True)
    player = User(first_name="Pat", last_name="Player", email="pat@x.com",
                  password_hash="x", role=Role.player, is_active=True)
    season = Season(year=2026, is_active=True)
    db.add_all([admin, player, season])
    db.commit()
    week = Week(
        season_id=season.id, week_number=5, label="Week 5",
        first_kickoff=datetime(2026, 10, 11, 17, 0, 0),
    )
    db.add(week)
    db.commit()
    ids = {"admin": admin.id, "player": player.id, "week": week.id}
    db.close()
    return ids


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def test_lead_time_is_a_setting_not_a_fixed_hour():
    ids = fresh()
    html = client_for(ids["admin"]).get("/settings").text
    assert "Picks webhook" in html
    assert "1 hour" in html
    assert "24 hours" in html
    saved = client_for(ids["admin"]).post(
        "/settings/picks-webhook",
        data={"webhook_url": "https://example.test/hook", "webhook_secret": "", "offset_minutes": 120},
    )
    assert saved.status_code == 200
    assert "Signing secret" in saved.text
    db = SessionLocal()
    try:
        assert picks_webhook.offset_minutes(db) == 120
        assert picks_webhook.config(db)["url"] == "https://example.test/hook"
        assert picks_webhook.config(db)["secret_set"]
    finally:
        db.close()
    # blank secret keeps the current one; the page must not echo it back
    marker = "Signing secret"
    block = saved.text.split(marker, 1)[1]
    secret = block.split('value="', 1)[1].split('"', 1)[0]
    again = client_for(ids["admin"]).post(
        "/settings/picks-webhook",
        data={"webhook_url": "https://example.test/hook", "webhook_secret": "", "offset_minutes": 30},
        follow_redirects=True,
    )
    assert secret not in again.text
    db = SessionLocal()
    try:
        assert picks_webhook.offset_minutes(db) == 30
        assert picks_webhook._get(db, picks_webhook.SETTING_SECRET) == secret
    finally:
        db.close()


def test_player_cannot_save_the_webhook():
    ids = fresh()
    resp = client_for(ids["player"]).post(
        "/settings/picks-webhook",
        data={"webhook_url": "https://example.test/hook", "offset_minutes": 60},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db = SessionLocal()
    try:
        assert picks_webhook.config(db)["url"] == ""
    finally:
        db.close()


def test_fires_once_inside_the_chosen_window_and_is_signed():
    ids = fresh()
    db = SessionLocal()
    picks_webhook.save_config(db, "https://example.test/hook", "topsecret", 60)
    seen = []

    async def poster(url, secret, body, delivery_id):
        seen.append((url, secret, body, delivery_id))
        return True, "HTTP 200"

    kickoff = datetime(2026, 10, 11, 17, 0, 0)
    # too early
    assert __import__("asyncio").get_event_loop().run_until_complete(
        picks_webhook.fire_due(db, now=kickoff - timedelta(hours=2), poster=poster)
    ) == 0
    assert seen == []
    # inside the hour
    assert __import__("asyncio").get_event_loop().run_until_complete(
        picks_webhook.fire_due(db, now=kickoff - timedelta(minutes=30), poster=poster)
    ) == 1
    url, secret, body, delivery_id = seen[0]
    assert url == "https://example.test/hook"
    assert delivery_id == "2026-W5"
    payload = json.loads(body)
    assert payload["offset_minutes"] == 60
    assert payload["week_number"] == 5
    assert payload["season_year"] == 2026
    assert payload["first_kickoff"] == "2026-10-11T17:00:00Z"
    expected = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert picks_webhook.sign(secret, body) == expected
    # second tick does not send again
    assert __import__("asyncio").get_event_loop().run_until_complete(
        picks_webhook.fire_due(db, now=kickoff - timedelta(minutes=10), poster=poster)
    ) == 0
    assert len(seen) == 1
    db.close()


def test_failed_delivery_is_retried_next_tick():
    ids = fresh()
    db = SessionLocal()
    picks_webhook.save_config(db, "https://example.test/hook", "topsecret", 60)
    calls = {"n": 0}

    async def poster(url, secret, body, delivery_id):
        calls["n"] += 1
        return False, "HTTP 500"

    kickoff = datetime(2026, 10, 11, 17, 0, 0)
    now = kickoff - timedelta(minutes=20)
    assert __import__("asyncio").get_event_loop().run_until_complete(
        picks_webhook.fire_due(db, now=now, poster=poster)
    ) == 0
    week = db.query(Week).filter(Week.id == ids["week"]).one()
    assert week.picks_webhook_sent is False
    assert calls["n"] == 1
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
