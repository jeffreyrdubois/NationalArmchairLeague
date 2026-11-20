"""SQLite startup and score-sync status must survive Unraid restart glitches.

The production failure mode after a container restart: GETs still return 200,
but the scheduled score sync cannot write ``score_sync_status`` and logs
``unable to open database file`` every 30 seconds. These tests pin the
defences — empty DATABASE_URL must not win, the data dir must be created, and
a cantopen on the caller session must retry on a fresh connection.

Each case that reloads ``app.database`` runs in a subprocess so it cannot
poison later tests (or the rest of the suite) with a different engine.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)


def _run_isolated(script: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"isolated test failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def test_empty_database_url_falls_back_to_data_dir():
    """Compose passes DATABASE_URL= even when unset; that must not become the URL."""
    data = tempfile.mkdtemp(prefix="nal-empty-url-")
    _run_isolated(f"""
import os
from pathlib import Path
os.environ["DATABASE_URL"] = "   "
os.environ["DATA_DIR"] = {data!r}
import app.database as database
url = str(database.engine.url)
assert url.startswith("sqlite:///"), url
assert url.endswith("/nal.db"), url
assert Path({data!r}).resolve().as_posix() in url.replace("\\\\", "/"), url
assert Path({data!r}, "nal.db").parent.exists()
print("ok")
""")


def test_assert_sqlite_writable_rejects_readonly_dir():
    data = tempfile.mkdtemp(prefix="nal-ro-")
    _run_isolated(f"""
import os
os.environ["DATABASE_URL"] = {f"sqlite:///{data}/nal.db"!r}
os.environ.pop("DATA_DIR", None)
os.chmod({data!r}, 0o555)
import app.database as database
try:
    ok = database.check_sqlite_writable()
finally:
    os.chmod({data!r}, 0o755)
assert ok is False, "readonly data dir must fail the startup probe without crashing"
print("ok")
""")


def test_init_db_still_starts_when_volume_is_not_writable():
    """A bad volume must not take the league offline — reads still matter."""
    data = tempfile.mkdtemp(prefix="nal-ro-init-")
    _run_isolated(f"""
import os
os.environ["DATABASE_URL"] = {f"sqlite:///{data}/nal.db"!r}
os.environ.pop("DATA_DIR", None)
# Create the db as a writable user first, then lock the directory.
import app.database as database
from app.database import Base
import app.models  # noqa: F401
Base.metadata.create_all(bind=database.engine)
os.chmod({data!r}, 0o555)
try:
    database.init_db()  # must not raise
finally:
    os.chmod({data!r}, 0o755)
print("ok")
""")


def test_record_score_sync_status_retries_on_cantopen():
    """A stale caller session that raises cantopen still gets the status saved."""
    db_path = os.path.join(tempfile.mkdtemp(prefix="nal-retry-"), "test.db")
    _run_isolated(f"""
import os
os.environ["DATABASE_URL"] = {f"sqlite:///{db_path}"!r}
import app.database as database
from app.database import Base, SessionLocal
import app.models  # noqa: F401
from app.services import scheduler

Base.metadata.create_all(bind=database.engine)
db = SessionLocal()
status = {{
    "ran_at": "2026-11-19T23:40:46",
    "trigger": "scheduled",
    "weeks": [],
    "error": "No week has kicked off yet",
}}

def flaky_commit():
    raise Exception("(sqlite3.OperationalError) unable to open database file")

db.commit = flaky_commit
scheduler.record_score_sync_status(db, status)

stored = scheduler.get_score_sync_status(SessionLocal())
assert stored is not None, "retry on a fresh session must persist the status"
assert stored["trigger"] == "scheduled", stored
assert stored["error"] == "No week has kicked off yet", stored
db.close()
print("ok")
""")


def test_is_sqlite_cantopen_matches_production_message():
    from app.services.scheduler import _is_sqlite_cantopen

    assert _is_sqlite_cantopen(
        Exception("(sqlite3.OperationalError) unable to open database file")
    )
    assert not _is_sqlite_cantopen(Exception("database is locked"))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")
