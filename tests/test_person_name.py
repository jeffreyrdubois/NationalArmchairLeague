"""Player names are text. Angle brackets are refused at the door, and a
name already in the database cannot break out of the admin delete confirm.

Run with: python tests/test_person_name.py
"""
import os
import re
import sys
import tempfile
from html import unescape
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(tempfile.mkdtemp(prefix="nal-name-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.database import Base, SessionLocal, engine
from app.models import Role, User
from app.routers import admin, auth
from app.utils import clean_person_name

app = FastAPI()
app.include_router(auth.router)
app.include_router(admin.router)


def reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def client_for(user_id):
    return TestClient(
        app,
        cookies={"access_token": create_access_token(user_id)},
        follow_redirects=False,
    )


def test_clean_person_name_strips_and_allows_apostrophes():
    assert clean_person_name("  O'Brien  ", "Last name") == "O'Brien"
    assert clean_person_name('Say "Jay"', "First name") == 'Say "Jay"'
    for bad in ("<script>", "</script>", "a<b", "a>b"):
        try:
            clean_person_name(bad, "First name")
        except ValueError as exc:
            assert "can't include" in str(exc)
        else:
            raise AssertionError(f"accepted {bad!r}")


def test_registration_rejects_angle_brackets_and_keeps_apostrophes():
    reset()
    client = TestClient(app, follow_redirects=False)
    blocked = client.post("/register", data={
        "first_name": "</script><script>alert(1)</script>",
        "last_name": "Player",
        "email": "evil@x.com",
        "password": "password1",
        "password2": "password1",
    })
    assert blocked.status_code == 400
    # The apostrophe in the message is HTML-escaped, and so is the attempt.
    assert "First name can&#39;t include" in blocked.text
    assert "&lt;/script&gt;&lt;script&gt;alert(1)" in blocked.text
    assert "<script>alert" not in blocked.text
    db = SessionLocal()
    assert db.query(User).count() == 0
    db.close()

    ok = client.post("/register", data={
        "first_name": "Sean",
        "last_name": "O'Brien",
        "email": "sean@x.com",
        "password": "password1",
        "password2": "password1",
    })
    assert ok.status_code == 303
    db = SessionLocal()
    user = db.query(User).filter(User.email == "sean@x.com").one()
    assert user.last_name == "O'Brien"
    db.close()


def test_admin_create_rejects_angle_brackets():
    reset()
    db = SessionLocal()
    boss = User(
        first_name="Ada", last_name="Admin", email="ada@x.com",
        password_hash="x", role=Role.admin,
    )
    db.add(boss)
    db.commit()
    boss_id = boss.id
    db.close()

    resp = client_for(boss_id).post("/admin/users/add", data={
        "first_name": "Pat",
        "last_name": "Player<img>",
        "email": "pat@x.com",
        "password": "password1",
    })
    assert resp.status_code == 303
    assert "Last name" in unquote(resp.headers["location"])
    db = SessionLocal()
    assert db.query(User).filter(User.email == "pat@x.com").first() is None
    db.close()


def test_stored_name_cannot_break_out_of_delete_confirm():
    """A row saved before the check still cannot inject an onsubmit handler."""
    reset()
    first = "</script><script>alert(1)</script>"
    last = "');alert(1)//"
    db = SessionLocal()
    boss = User(
        first_name="Ada", last_name="Admin", email="ada@x.com",
        password_hash="x", role=Role.admin,
    )
    attacker = User(
        first_name=first, last_name=last, email="evil@x.com",
        password_hash="x", role=Role.player,
    )
    db.add_all([boss, attacker])
    db.commit()
    boss_id = boss.id
    db.close()

    page = client_for(boss_id).get("/admin/")
    assert page.status_code == 200
    html = page.text
    assert "</script><script>" not in html
    assert "onsubmit=\"return confirm('Permanently delete" not in html

    tag = re.search(
        r'<form method="post" action="/admin/users/\d+/delete"[^>]*>',
        html,
    ).group(0)
    assert "onsubmit" not in tag
    attr = re.search(r'data-user-name="([^"]*)"', tag).group(1)
    assert unescape(attr) == f"{first} {last}"
    assert "form.js-delete-user" in html
    assert "slot.textContent" in html


if __name__ == "__main__":
    test_clean_person_name_strips_and_allows_apostrophes()
    test_registration_rejects_angle_brackets_and_keeps_apostrophes()
    test_admin_create_rejects_angle_brackets()
    test_stored_name_cannot_break_out_of_delete_confirm()
    print("ok")
