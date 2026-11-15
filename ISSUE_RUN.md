# Issue run instructions

Read this file once at the start of any issue run. Do not re-read it mid-run. Do not read `AGENTS.md` or `MANUAL.md` unless this file explicitly points you there for a specific area.

Handle ONE issue per run. The webhook is the only owner. A new comment is additional instruction on the same issue, not a new issue. Read that comment before you classify or continue. Do not open a second PR.

**Merge rule (non-negotiable).** GitHub does not auto-merge eligible issue PRs. When the issue is **not** `NeedsJeffrey` and the change is auto-merge eligible (class A, class B, or clean app-only work), **you** squash-merge in the **same run** after CI is green. Ending with an open, unmerged PR is a failed run — Jeffrey should not have to merge manually. Optional walkthrough or screenshots never justify stopping before merge.

Every comment this run posts must end with the exact marker `<!-- nal-grok -->` on its own line. Without it, the comment wakes another run.

**Started.** As soon as the event is in scope, and before any other work:

1. Comment once:

    Cursor: Looking into this

    <!-- nal-grok -->

2. Immediately add the in-progress label (label API only — do **not** put this in a comment):

    gh api -X POST repos/jeffreyrdubois/NationalArmchairLeague/issues/<n>/labels -f "labels[]=IssueRunInProgress"

The label tells the scheduled issue-run watchdog that a run is still active so it does not start a second automation while you are working. Remove it on every exit path below.

Do not skip started on a proceed. Do not post started on an ignored event.

**Watchdog retry (`watchdog_retry: true` in webhook context).** A stalled run may be nudged once per day by `.github/workflows/issue-run-watchdog.yml`. On that event:

- Do not post **Started** again if the issue already has `Cursor: Looking into this` with the sentinel.
- Do not add `IssueRunInProgress` again if it is already on the issue.
- If a class/plan comment already exists, do not re-classify; fetch `origin` and continue.
- If `auto/issue-<n>-*` or an open PR already exists, treat this as a **follow-up run** only — never open a second PR.

**Follow-up runs (existing branch or open PR).**  
If an `auto/issue-<n>-*` branch or open pull request already exists for this number:

- Do not re-read area docs or this file beyond what you already have.
- Do not re-classify or rebuild a closed file list.
- Fetch only the new comment, the open PR (diff or files changed), and current CI status (`gh pr checks` or the failing run log).
- Address only what the new comment asks. Pure status checks (“any update?”, “is CI green?”, “CI failed again” with no new requirement) reply with the current PR link, class, CI status, and NeedsJeffrey state, then stop. Do not edit files.
- **Unmerged eligible PR (no new code).** If the open PR is class A/B (or app-only), the issue has no `NeedsJeffrey`, and CI is green: mark the PR ready if it is still draft, `gh pr merge --squash --delete-branch`, and close out with `merged: yes` — even when the follow-up comment did not say “merge”. Do not wait for Jeffrey.
- If the comment changes the work (new requirement, explicit CI or conflict fix), make the minimal edit on the existing branch, run the local gate, and push once. Still no second PR.
- If the comment says `resume`, `merge when green`, or similar and the PR is still **draft** on class A/B, mark it ready, wait for green CI, squash-merge, and close out — do not reopen classification.
- After two fix-up pushes total, apply `NeedsJeffrey` if not already present and stop.

Full classification and the read steps below apply only on the first run (or when no branch/PR exists yet).

**Read budget (first run only).** After this file, read only what the issue names:

- Route / page / template → the specific file under `app/routers/`, `app/templates/`, or `app/static/`.
- Scoring, picks, visibility, payouts, webhook, MCP, updater → the matching module under `app/services/` (and its test under `tests/` if one exists).
- Schema / SQLite migration → `app/models.py` and `app/database.py` (`_migrate`).
- Docker / Unraid / compose / workflows → that file only (`Dockerfile`, `docker-compose.yml`, `unraid/`, `.github/workflows/`).
- Player-facing how-to or admin setup → the relevant section of `MANUAL.md` only when the issue is about docs or setup copy.

Do not read the whole MANUAL unless the issue is documentation. Stop after the named files unless classify proves the change crosses a folder.

**NeedsJeffrey.** Apply when this run cannot finish: class C, D, or E; the two-push CI cap; or a blocker only Jeffrey can clear (secrets, Unraid host steps, Cursor automation webhook credentials). The PR must already be open. A plan comment with no PR is not a stop. Do not apply on a merged A or B. Do not remove it on the stopping run.

Do **not** apply NeedsJeffrey for: ordinary SQLite schema adds in `app/database.py` `_migrate` that run on container boot; optional post-deploy visual checks; unreadable CI on this runner when the PR is otherwise eligible — wait for CI with `gh pr checks`, PR `get_ci_status`, or `subscribe_github_ci`, then merge when green instead of stopping.

Apply and remove with the GitHub CLI (already logged in):

    gh api -X POST repos/jeffreyrdubois/NationalArmchairLeague/issues/<n>/labels -f "labels[]=NeedsJeffrey"
    gh api -X DELETE repos/jeffreyrdubois/NationalArmchairLeague/issues/<n>/labels/NeedsJeffrey

**Classify (first run).**  
A: pure UI/layout/copy in templates, static CSS/JS, or README/MANUAL with no model or `_migrate` change.  
B: small behavior in routers/services/tests that uses the existing schema (or additive `_migrate` columns that boot safely).  
C: destructive data rewrite, a migration that cannot roll forward safely on the live `/app/data` volume, or a change that must not publish until Jeffrey confirms timing (e.g. mid-week lock behavior).  
D: workflow, Unraid template, host script, or secret Jeffrey must copy, set, or restart outside a normal image pull.  
E: blocked on a question only Jeffrey can answer.

**Local gate (non-negotiable).**  
Never `git push` or open a PR until the local gate is green. Prefer the same checks CI runs:

```
pip install -r requirements.txt
python -c "from app.main import app; print(app.title)"
python - <<'PY'
from app.templates_config import templates
env = templates.env
names = env.list_templates(extensions=["html"])
for name in names:
    env.get_template(name)
print(f"compiled {len(names)} templates")
PY
python tests/test_pick_editing.py
# plus every other tests/test_*.py file your change can affect
```

When only a subset of tests is relevant, run those plus import and template compile. CI (`.github/workflows/ci.yml`) is the authoritative full gate. Before any fix-up push, reproduce the CI failure locally and confirm the applicable gate is green.

The close-out comment (or PR body) must include confirmation that the local gate passed, for example:

    Local gate green: import + templates + relevant tests

**Work (class A or B).**  
Branch `auto/issue-<n>-<slug>` from main **before** posting the class/plan comment on the issue. Until that branch exists locally, do not post a plan comment (put the plan in the PR body instead if you need notes before push).  

After the branch exists, comment a short plan: the one behavior, the closed file list, how you will test, whether you expect to merge.  

Before `git push`, run `git fetch origin` and confirm there is still no other `auto/issue-<n>-*` on the remote — if one exists, check it out and continue there; do not open a second PR.  

Edit only the named files. Prefer surgical edits.  
If the body asks for two behaviors, ship the first complete one and comment that the rest needs its own issue.  
Run the local gate before any push.  
Open a PR titled `<summary> (#n)` with `Fixes #n` **after** the branch is complete and the local gate is green — not as an early checkpoint before merge. Include the local gate confirmation in the PR body. Do not write “Do not auto-merge” unless NeedsJeffrey applies. Do not add a **Review** section or plan for manual owner review unless NeedsJeffrey applies; optional post-merge smoke notes belong under `## Deploy notes`.

**Draft vs ready (auto-merge eligible).** Class A, class B, and ordinary app-only features open the PR **ready for review** (`draft: false`). **Never** pass `draft: true` to `ManagePullRequest` on an eligible issue. `gh pr create` must not use `--draft`. If a tool defaults to draft, call `update_pr` with `draft: false` or `gh pr ready` **before** waiting on CI. **Draft PRs are only for class C/D/E stops** (NeedsJeffrey). An eligible issue left open as draft is a failed run.

**Pre-merge testing (auto-merge eligible).** The local gate and tests for the changed files satisfy pre-merge testing. Browser screenshots and walkthrough artifacts are optional and **after** merge and close-out, or in parallel while CI runs — never between “CI green” and `gh pr merge`. Optional post-deploy visual checks do not block merge.

**Auto-merge (class A, class B, or clean app-only).**  
Same run, in order: push → open **ready** PR (`draft: false`) → wait for CI (`gh pr checks`, check runs, `get_ci_status`, or `subscribe_github_ci`) → `gh pr merge --squash --delete-branch` → issue close-out with `merged: yes` → optional post-merge walkthrough. Do not stop the run with an open PR for Jeffrey to merge. An open unmerged PR at end of run on an eligible issue is a **failed run**, not success.

Squash-merge when CI is green and **all** of the following hold:

- Class A or class B (above)
- Not class C/D/E, not past the two-push CI cap, no host/secret step only Jeffrey can clear
- No mid-season lock or payout change that class C reserved for Jeffrey’s timing

    gh pr merge <n> --squash --delete-branch

Do not write “Do not auto-merge” in the PR body. Close out only after the merge has succeeded and report `merged: yes`. Leave the PR open for manual merge only when NeedsJeffrey applies.

**Stop conditions.** On class C/D/E, open the draft PR first, then apply `NeedsJeffrey`, then comment why and stop. Do not continue past the stop.

**CI red.** Reproduce the failure locally first. Fix it. Run the applicable local gate and confirm it is green. Then one push. Second failure: one more push after another local green confirmation. After two fix-up pushes, comment `stopped: need Jeffrey`, apply `NeedsJeffrey`, and stop. Do not start a third push.

**Before you close out (eligible merge).** If class is A or B, the issue does not have `NeedsJeffrey`, and CI is green: confirm the PR is not draft, run `gh pr merge --squash --delete-branch`, then post the footer with `merged: yes`. If you skip merge on an eligible issue, the run failed.

**Close out (mandatory on every exit).** Every run must end with the footer comment below — success, `stopped: need Jeffrey`, class C/D/E stop, or unrecoverable error. A plan with no PR and no footer is a failed run; the watchdog may re-trigger once after the stall interval.

Remove `IssueRunInProgress` when you close out:

    gh api -X DELETE repos/jeffreyrdubois/NationalArmchairLeague/issues/<n>/labels/IssueRunInProgress

Comment the PR link and outcome. Include the local gate confirmation line when a PR exists. If waiting or stopped, the PR must already be open and `NeedsJeffrey` must already be on the issue. If the conditions for auto-merge were met, the PR must already be merged. End with:

    issue: #<n> | PR: <url|none> | class: <A-E> | merged: yes/no | NeedsJeffrey: yes/no

If the run cannot open a PR, still post the footer with `PR: none` and `merged: no` so the watchdog does not loop forever.

Never deploy by hand. After a merge, say an image update is ready (`ghcr.io/jeffreyrdubois/nationalarmchairleague:latest`) for a quiet moment — Unraid Apply, Compose `./update.sh`, or Admin → Update the App.
