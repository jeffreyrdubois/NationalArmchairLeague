# National Armchair League: instructions for the next maintainer (AI)

You are taking over maintenance of `jeffreyrdubois/NationalArmchairLeague`. For a newly opened, reopened, or commented issue, read **ISSUE_RUN.md only**, then the specific files it names. Do not follow every pointer in this file on that run. For any other change, read this whole file before you edit.

---

## 1. What this is

National Armchair League (NAL) is a weekly NFL confidence-pick game for a family league. Players submit confidence-point picks before kickoff; scores and standings update through the season; prize payouts and payment preferences live in the admin tools.

The owner is Jeffrey (GitHub `jeffreyrdubois`). Players and admins can file change requests through **Submit an Issue** (`/feedback`), so many issues arrive already linked to a screen and build.

When an issue run cannot finish the work itself, it opens the pull request first, then applies the existing `NeedsJeffrey` label. That label is how Jeffrey finds issues waiting on him. Do not apply it when the issue was merged, and do not apply it before the pull request exists (except when a host/secret step is the whole stop — still open a PR when code or docs changed).

## 2. Where things live

| Path | What it is |
|---|---|
| `app/` | FastAPI app: routers, services, templates, static assets, MCP server |
| `app/models.py` / `app/database.py` | SQLAlchemy models and boot-time SQLite `_migrate` |
| `tests/` | CI gate tests (import, templates, picks, visibility, updater, MCP, …) |
| `unraid/` | Unraid container template |
| `Dockerfile` / `docker-compose.yml` / `update.sh` | Image and compose deploy |
| `MANUAL.md` | Player and admin how-to (authoritative for product behavior) |
| `.github/workflows/` | `ci.yml`, `docker-publish.yml`, `grok-new-issue.yml`, `issue-run-watchdog.yml` |

## 3. Database

One SQLite database under `/app/data` (local default `./data/nal.db`). Schema starts from `Base.metadata.create_all`, then incremental changes in `app/database.py` `_migrate`. Additive column migrations that run safely on existing volumes are normal app work and do **not** by themselves force `NeedsJeffrey`. Destructive rewrites or mid-season behavior that needs Jeffrey’s timing are class C (see `ISSUE_RUN.md`).

## 4. CI and shipping

Local / CI gate (see `.github/workflows/ci.yml`): install requirements, import the app, compile every Jinja template, run the `tests/` suite. Never push or open a PR until the applicable local gate is green.

Merging to `main` publishes `ghcr.io/jeffreyrdubois/nationalarmchairleague:latest` (amd64). Production updates via Unraid Apply, `./update.sh`, or Admin → Update the App.

## 5. Issue handling automations

Opened issues, reopened issues, and new issue comments are forwarded by `.github/workflows/grok-new-issue.yml` to the Cursor automation webhook (`CURSOR_WEBHOOK_URL` + `CURSOR_AUTOMATION_KEY` repository secrets). Pull-request comments, bot comments, and comments containing `<!-- nal-grok -->` are not forwarded. Comments under 40 characters, status-only comments, and comments on `NeedsJeffrey` issues are forwarded only when the comment contains a resume phrase (`resume`, `continue`, `migration ran`, `ran the migration`, `fix the ci`, `fix ci`, `ci failed`, `conflicts`, `conflict`, `please fix`, `update applied`).

`.github/workflows/issue-run-watchdog.yml` runs on a schedule and nudges Cursor **once** when an issue has triage but no PR, no `auto/issue-<n>-*` branch on the remote, and no close-out footer — with longer waits when `IssueRunInProgress` is set so a slow run is not duplicated. The watchdog creates its labels (`IssueRunInProgress`, `IssueRunWatchdogFired`) if missing. Issue runs manage those labels per `ISSUE_RUN.md`.

**Issue runs read ISSUE_RUN.md.** That file is the complete short contract for an issue run. Read it once at the start. Do not re-read this file or the whole MANUAL on an issue run unless ISSUE_RUN.md points you there.

## 6. Pull request discipline

An issue that receives work must end with either (1) a PR opened for Jeffrey when `NeedsJeffrey` applies, or (2) an eligible PR squash-merged in the same run after green CI. Class A/B ready PRs must not be left draft. One issue, one `auto/issue-<n>-*` branch, one PR.
