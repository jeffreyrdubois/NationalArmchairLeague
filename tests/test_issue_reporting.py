"""Tests for configuring "Submit an Issue" -> GitHub from the Admin Panel.

Two things matter here. The first is precedence: a value saved in the app has
to beat the container's environment variable, or the admin page would look
like it worked while reports kept going to the old repo. The second is that
the token is a credential — it must never come back out of the app, not in a
rendered page and not in the audit log, because both are read by people who
should not end up holding it.

Run with: python tests/test_issue_reporting.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-issues-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import AppSetting, AuditLog, Role, User
from app.routers import admin, feedback
from app.services import github_issues

app = FastAPI()
app.include_router(admin.router)
app.include_router(feedback.router)

SAVED_TOKEN = "github_pat_SAVEDinTHEapp1234"
ENV_TOKEN = "github_pat_FROMtheENVfile5678"


def fresh_league():
    """An empty database with one admin and one player, and no env config."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    os.environ.pop("GITHUB_ISSUE_TOKEN", None)
    os.environ.pop("GITHUB_ISSUE_REPO", None)

    db = SessionLocal()
    admin_user = User(first_name="Admin", last_name="Ace", email="admin@x.com",
                      password_hash="x", role=Role.admin)
    player = User(first_name="Pat", last_name="Player", email="pat@x.com",
                  password_hash="x", role=Role.player)
    db.add_all([admin_user, player])
    db.commit()
    ids = {"admin": admin_user.id, "player": player.id}
    db.close()
    return ids


def client_for(user_id):
    return TestClient(app, cookies={"access_token": create_access_token(user_id)})


def stub_verify(ok, message):
    """Stand in for the GitHub round trip — no network in the test suite."""
    async def _verify(token, repo):
        return ok, message
    return _verify


def audit_details():
    db = SessionLocal()
    try:
        return " ".join(log.detail or "" for log in db.query(AuditLog).all())
    finally:
        db.close()


# --------------------------------------------------------------------------
# Where the settings come from
# --------------------------------------------------------------------------

def test_unconfigured_app_reports_itself_unconfigured():
    fresh_league()
    db = SessionLocal()
    config = github_issues.get_config(db)
    assert config["configured"] is False, "an app with no token claimed to be configured"
    assert config["repo"] == github_issues.DEFAULT_REPO
    assert config["token_source"] is None
    db.close()


def test_environment_variables_still_work():
    fresh_league()
    os.environ["GITHUB_ISSUE_TOKEN"] = ENV_TOKEN
    os.environ["GITHUB_ISSUE_REPO"] = "someone/from-env"

    db = SessionLocal()
    config = github_issues.get_config(db)
    assert config["configured"] is True, "env-configured app was treated as unconfigured"
    assert config["token"] == ENV_TOKEN
    assert config["repo"] == "someone/from-env"
    assert config["token_source"] == "env"
    db.close()


def test_saved_settings_beat_the_environment():
    fresh_league()
    os.environ["GITHUB_ISSUE_TOKEN"] = ENV_TOKEN
    os.environ["GITHUB_ISSUE_REPO"] = "someone/from-env"

    db = SessionLocal()
    github_issues.save_config(db, token=SAVED_TOKEN, repo="someone/from-app")
    config = github_issues.get_config(db)
    assert config["token"] == SAVED_TOKEN, "the environment overrode a value saved in the app"
    assert config["repo"] == "someone/from-app"
    assert config["token_source"] == "app"
    db.close()


def test_clearing_falls_back_to_the_environment():
    fresh_league()
    os.environ["GITHUB_ISSUE_TOKEN"] = ENV_TOKEN
    os.environ["GITHUB_ISSUE_REPO"] = "someone/from-env"

    db = SessionLocal()
    github_issues.save_config(db, token=SAVED_TOKEN, repo="someone/from-app")
    github_issues.clear_config(db)
    config = github_issues.get_config(db)
    assert config["token"] == ENV_TOKEN, "clearing left the app's token behind"
    assert config["repo"] == "someone/from-env"
    db.close()


def test_a_blank_token_keeps_the_saved_one():
    fresh_league()
    db = SessionLocal()
    github_issues.save_config(db, token=SAVED_TOKEN, repo="someone/first")
    github_issues.save_config(db, token="", repo="someone/second")
    config = github_issues.get_config(db)
    assert config["token"] == SAVED_TOKEN, "changing only the repo wiped the token"
    assert config["repo"] == "someone/second"
    db.close()


# --------------------------------------------------------------------------
# The admin page
# --------------------------------------------------------------------------

def test_admin_can_save_settings_and_the_token_never_comes_back():
    ids = fresh_league()
    admin.github_issues.verify = stub_verify(True, "Connected to someone/repo.")
    client = client_for(ids["admin"])

    resp = client.post("/admin/github",
                       data={"github_repo": "someone/repo", "github_token": SAVED_TOKEN},
                       follow_redirects=False)
    assert resp.status_code == 303, f"saving returned {resp.status_code}"

    db = SessionLocal()
    config = github_issues.get_config(db)
    db.close()
    assert config["token"] == SAVED_TOKEN and config["repo"] == "someone/repo", "settings were not saved"

    html = client.get("/admin/").text
    assert SAVED_TOKEN not in html, "the admin page rendered the token back out"
    assert github_issues.mask_token(SAVED_TOKEN) in html, "no masked hint that a token is saved"
    assert "someone/repo" in html, "the saved repository is not shown"
    assert SAVED_TOKEN not in audit_details(), "the token was written to the audit log"


def test_settings_that_github_rejects_are_not_saved():
    ids = fresh_league()
    admin.github_issues.verify = stub_verify(False, "GitHub rejected the token.")
    client = client_for(ids["admin"])

    client.post("/admin/github",
                data={"github_repo": "someone/typo", "github_token": "bad-token"},
                follow_redirects=False)

    db = SessionLocal()
    saved = {row.key for row in db.query(AppSetting).all()}
    db.close()
    assert github_issues.SETTING_TOKEN not in saved, "a token GitHub rejected was saved anyway"


def test_clear_button_removes_the_saved_settings():
    ids = fresh_league()
    admin.github_issues.verify = stub_verify(True, "ok")
    client = client_for(ids["admin"])

    client.post("/admin/github",
                data={"github_repo": "someone/repo", "github_token": SAVED_TOKEN},
                follow_redirects=False)
    client.post("/admin/github/clear", follow_redirects=False)

    db = SessionLocal()
    saved = {row.key for row in db.query(AppSetting).all()}
    configured = github_issues.is_configured(db)
    db.close()
    assert github_issues.SETTING_TOKEN not in saved, "clearing left the token in the database"
    assert configured is False, "the app still reports issue reporting as configured"


def test_players_cannot_touch_the_settings():
    ids = fresh_league()
    admin.github_issues.verify = stub_verify(True, "ok")
    client = client_for(ids["player"])

    for path in ("/admin/github", "/admin/github/test", "/admin/github/clear"):
        resp = client.post(path, data={"github_repo": "sneaky/repo", "github_token": "nope"},
                           follow_redirects=False)
        assert resp.status_code == 403, f"a player got {resp.status_code} from {path}"

    db = SessionLocal()
    saved = {row.key for row in db.query(AppSetting).all()}
    db.close()
    assert not saved, "a player changed the issue-reporting settings"


def test_feedback_page_follows_the_saved_settings():
    ids = fresh_league()
    admin.github_issues.verify = stub_verify(True, "ok")

    html = client_for(ids["player"]).get("/feedback").text
    assert "isn&#39;t set up" in html or "isn't set up" in html, \
        "the feedback page did not say reporting is unconfigured"

    client_for(ids["admin"]).post(
        "/admin/github",
        data={"github_repo": "someone/repo", "github_token": SAVED_TOKEN},
        follow_redirects=False,
    )
    html = client_for(ids["player"]).get("/feedback").text
    assert "isn't set up" not in html and "isn&#39;t set up" not in html, \
        "the feedback page still says reporting is unconfigured after it was set up"
    assert SAVED_TOKEN not in html, "the feedback page leaked the token"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"{len(tests) - failures} passed, {failures} failed")
    sys.exit(1 if failures else 0)
