#!/usr/bin/env python3
"""Unit tests for issue-run watchdog helpers (no GitHub calls)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from issue_run_watchdog import (
    LABEL_IN_PROGRESS,
    CLOSE_OUT_RE,
    CLASSIFY_RE,
    STARTED_RE,
    stall_reason,
    stall_reference_time,
    triage_timestamps,
)


class WatchdogLogicTest(unittest.TestCase):
    def test_triage_parses_sentinel_comments(self) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        comments = [
            {
                "body": "Cursor: Looking into this\n\n<!-- nal-grok -->",
                "created_at": now,
            },
            {
                "body": "Class **A** — example\n\n<!-- nal-grok -->",
                "created_at": now,
            },
        ]
        triage = triage_timestamps(comments)
        self.assertIsNotNone(triage["started"])
        self.assertIsNotNone(triage["classified"])
        self.assertIsNone(triage["close_out"])

    def test_close_out_stops_stall_clock(self) -> None:
        comments = [
            {
                "body": "issue: #1 | PR: none | class: A | merged: no | NeedsJeffrey: no\n\n<!-- nal-grok -->",
                "created_at": "2026-11-08T04:00:00Z",
            }
        ]
        triage = triage_timestamps(comments)
        ref = stall_reference_time(triage, [])
        self.assertIsNone(ref)

    def test_in_progress_extends_grace(self) -> None:
        ref = datetime.now(timezone.utc) - timedelta(minutes=60)
        reason = stall_reason(888, [LABEL_IN_PROGRESS], ref)
        self.assertIsNotNone(reason)
        self.assertIn("grace", reason)

    def test_stall_after_threshold_without_label(self) -> None:
        ref = datetime.now(timezone.utc) - timedelta(minutes=60)
        reason = stall_reason(888, [], ref)
        self.assertIsNone(reason)

    def test_regexes(self) -> None:
        self.assertTrue(CLASSIFY_RE.search("Class **B** — thing"))
        self.assertTrue(STARTED_RE.search("Cursor: Looking into this"))
        self.assertTrue(CLOSE_OUT_RE.search("issue: #9 | PR: none | class: A"))


if __name__ == "__main__":
    unittest.main()
