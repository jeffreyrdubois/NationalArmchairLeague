#!/usr/bin/env python3
"""
Detect issue runs that posted triage/classification but never opened a PR,
and nudge the Cursor automation once — without colliding with an in-flight run.

See ISSUE_RUN.md (IssueRunInProgress / watchdog) and issue-run-watchdog.yml.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

REPO = "jeffreyrdubois/NationalArmchairLeague"
OWNER, NAME = REPO.split("/", 1)

LABEL_IN_PROGRESS = "IssueRunInProgress"
LABEL_WATCHDOG_FIRED = "IssueRunWatchdogFired"

CLOSE_OUT_RE = re.compile(
    r"issue:\s*#(?P<num>\d+)\s*\|\s*PR:",
    re.IGNORECASE,
)
CLASSIFY_RE = re.compile(r"Class\s+\*\*[A-E]\*\*", re.IGNORECASE)
STARTED_RE = re.compile(r"Cursor:\s*Looking into this", re.IGNORECASE)
SENTINEL = "<!-- nal-grok -->"


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


STALL_MINUTES = env_int("ISSUE_RUN_STALL_MINUTES", 45)
IN_PROGRESS_GRACE_MINUTES = env_int("ISSUE_RUN_IN_PROGRESS_GRACE_MINUTES", 120)
WATCHDOG_COOLDOWN_HOURS = env_int("ISSUE_RUN_WATCHDOG_COOLDOWN_HOURS", 24)
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def run_gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def parse_github_ts(iso: str) -> datetime:
    # GitHub uses Zulu timestamps, e.g. 2026-11-08T04:08:24Z
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


def minutes_since(ts: datetime) -> float:
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


def ensure_labels() -> None:
    """Create watchdog labels if missing (idempotent)."""
    specs = [
        (LABEL_IN_PROGRESS, "1d76db", "Cursor issue run is in progress; watchdog waits longer."),
        (LABEL_WATCHDOG_FIRED, "fbca04", "Watchdog already re-triggered this stalled issue once."),
    ]
    for label_name, color, description in specs:
        try:
            run_gh(
                [
                    "api",
                    "-X",
                    "POST",
                    f"repos/{REPO}/labels",
                    "-f",
                    f"name={label_name}",
                    "-f",
                    f"color={color}",
                    "-f",
                    f"description={description}",
                ]
            )
        except subprocess.CalledProcessError:
            pass


def list_open_issue_numbers() -> list[int]:
    out = run_gh(
        [
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number",
        ]
    )
    rows = json.loads(out or "[]")
    return [int(row["number"]) for row in rows]


def issue_comments(issue_number: int) -> list[dict[str, Any]]:
    out = run_gh(
        [
            "api",
            f"repos/{REPO}/issues/{issue_number}/comments",
            "--paginate",
        ]
    )
    return json.loads(out or "[]")


def issue_labels(issue_number: int) -> list[str]:
    out = run_gh(["api", f"repos/{REPO}/issues/{issue_number}", "--jq", ".labels[].name"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def has_open_pr(issue_number: int) -> bool:
    query = (
        "query($owner: String!, $name: String!, $n: Int!) {"
        " repository(owner: $owner, name: $name) {"
        " issue(number: $n) { linkedPullRequests(states: OPEN, first: 1) { totalCount } }"
        " } }"
    )
    try:
        out = run_gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-f",
                f"owner={OWNER}",
                "-f",
                f"name={NAME}",
                "-F",
                f"n={issue_number}",
            ]
        )
        data = json.loads(out)
        count = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("linkedPullRequests", {})
            .get("totalCount", 0)
        )
        return int(count) > 0
    except (subprocess.CalledProcessError, json.JSONDecodeError, TypeError):
        return False


def has_issue_branch(issue_number: int) -> bool:
    prefix = f"auto/issue-{issue_number}-"
    try:
        out = run_gh(
            [
                "api",
                f"repos/{REPO}/git/matching-refs/heads/{prefix}",
                "--jq",
                "length",
            ]
        )
        return int(out.strip() or "0") > 0
    except subprocess.CalledProcessError:
        return False


def last_watchdog_comment_at(comments: list[dict[str, Any]]) -> datetime | None:
    best: datetime | None = None
    for comment in comments:
        body = comment.get("body") or ""
        if "watchdog_retry" not in body:
            continue
        created = comment.get("created_at")
        if not created:
            continue
        ts = parse_github_ts(created)
        if best is None or ts > best:
            best = ts
    return best


def triage_timestamps(comments: list[dict[str, Any]]) -> dict[str, datetime | None]:
    started: datetime | None = None
    classified: datetime | None = None
    close_out: datetime | None = None
    for comment in comments:
        body = comment.get("body") or ""
        if SENTINEL not in body:
            continue
        created = comment.get("created_at")
        if not created:
            continue
        ts = parse_github_ts(created)
        if CLOSE_OUT_RE.search(body):
            if close_out is None or ts > close_out:
                close_out = ts
        if CLASSIFY_RE.search(body):
            if classified is None or ts > classified:
                classified = ts
        if STARTED_RE.search(body):
            if started is None or ts > started:
                started = ts
    return {"started": started, "classified": classified, "close_out": close_out}


def stall_reference_time(
    triage: dict[str, datetime | None],
    labels: list[str],
) -> datetime | None:
    """When the run clearly began but has not closed out."""
    if triage["close_out"] is not None:
        return None
    if triage["classified"] is not None:
        return triage["classified"]
    if triage["started"] is not None and LABEL_IN_PROGRESS in labels:
        return triage["started"]
    if triage["started"] is not None:
        return triage["started"]
    return None


def should_skip_cooldown(labels: list[str], comments: list[dict[str, Any]]) -> str | None:
    if LABEL_WATCHDOG_FIRED not in labels:
        return None
    last = last_watchdog_comment_at(comments)
    if last is None:
        return None
    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if hours < WATCHDOG_COOLDOWN_HOURS:
        return (
            f"watchdog nudged {hours:.1f}h ago (cooldown {WATCHDOG_COOLDOWN_HOURS}h)"
        )
    return None


def stall_reason(issue_number: int, labels: list[str], ref_time: datetime) -> str | None:
    age_min = minutes_since(ref_time)
    if LABEL_IN_PROGRESS in labels:
        if age_min < IN_PROGRESS_GRACE_MINUTES:
            return (
                f"#{issue_number}: IssueRunInProgress, only {age_min:.0f}m "
                f"(grace {IN_PROGRESS_GRACE_MINUTES}m)"
            )
    if age_min < STALL_MINUTES:
        return f"#{issue_number}: only {age_min:.0f}m since triage (stall {STALL_MINUTES}m)"
    return None


def build_webhook_context(issue_number: int, title: str, url: str) -> str:
    lines = [
        "event: issue_watchdog",
        "action: stalled_retry",
        "watchdog_retry: true",
        f"issue: {issue_number}",
        f"title: {title}",
        f"url: {url}",
        "",
        "An earlier issue run posted triage but never opened a PR or pushed",
        f"auto/issue-{issue_number}-*. Do not open a second PR if a branch or PR",
        "already exists. If context is stale, fetch GitHub first.",
        "",
        "Read ISSUE_RUN.md (watchdog retry). Read this issue with gh.",
        "No other issue is in scope.",
    ]
    return "\n".join(lines)


def post_webhook(context: str) -> None:
    url = os.environ.get("WEBHOOK_URL", "").strip()
    secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not url or not secret:
        raise SystemExit("Missing CURSOR_WEBHOOK_URL or CURSOR_AUTOMATION_KEY")

    payload = json.dumps({"context": context}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(response.status, response.read().decode()[:300])


def add_labels(issue_number: int, add: list[str]) -> None:
    if not add:
        return
    fields: list[str] = ["api", "-X", "POST", f"repos/{REPO}/issues/{issue_number}/labels"]
    for label in add:
        fields.extend(["-f", f"labels[]={label}"])
    run_gh(fields)


def post_watchdog_comment(issue_number: int) -> None:
    body = (
        "Issue-run watchdog: the earlier automation did not open a PR. "
        "Re-triggering Cursor once (no duplicate branch if one already exists).\n\n"
        "`watchdog_retry: true`\n\n"
        "<!-- nal-grok -->"
    )
    if DRY_RUN:
        print(f"DRY_RUN: would comment on #{issue_number}")
        return
    run_gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{REPO}/issues/{issue_number}/comments",
            "-f",
            f"body={body}",
        ]
    )


def process_issue(issue_number: int) -> None:
    meta_raw = run_gh(["api", f"repos/{REPO}/issues/{issue_number}"])
    meta = json.loads(meta_raw)
    title = meta.get("title") or ""
    url = meta.get("html_url") or ""

    labels = issue_labels(issue_number)
    comments = issue_comments(issue_number)
    triage = triage_timestamps(comments)

    if triage["close_out"] is not None:
        print(f"#{issue_number}: skip (close-out posted)")
        return

    ref = stall_reference_time(triage, labels)
    if ref is None:
        print(f"#{issue_number}: skip (no triage comment)")
        return

    skip = should_skip_cooldown(labels, comments)
    if skip:
        print(f"#{issue_number}: skip ({skip})")
        return

    if has_open_pr(issue_number):
        print(f"#{issue_number}: skip (open PR)")
        return

    if has_issue_branch(issue_number):
        print(f"#{issue_number}: skip (remote auto/issue-{issue_number}-* branch)")
        return

    wait = stall_reason(issue_number, labels, ref)
    if wait:
        print(wait)
        return

    context = build_webhook_context(issue_number, title, url)
    print(f"#{issue_number}: STALLED — posting watchdog webhook")
    if DRY_RUN:
        print(context[:400])
        return

    post_webhook(context)
    post_watchdog_comment(issue_number)
    add_labels(issue_number, [LABEL_WATCHDOG_FIRED])


def main() -> None:
    ensure_labels()
    issues = list_open_issue_numbers()
    print(
        f"Scanning {len(issues)} open issues "
        f"(stall={STALL_MINUTES}m, in-progress grace={IN_PROGRESS_GRACE_MINUTES}m, "
        f"cooldown={WATCHDOG_COOLDOWN_HOURS}h, dry_run={DRY_RUN})"
    )
    for number in issues:
        try:
            process_issue(number)
        except (subprocess.CalledProcessError, urllib.error.URLError) as err:
            print(f"#{number}: error {err}", file=sys.stderr)


if __name__ == "__main__":
    main()
