"""Overall standings on the pick card are a W-L record, ties included.

Run with: python tests/test_team_records.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.espn import format_record


def test_record_drops_a_zero_tie():
    assert format_record("4", "0", "0") == "4-0"


def test_record_keeps_a_tie():
    assert format_record("3", "1", "1") == "3-1-1"


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
