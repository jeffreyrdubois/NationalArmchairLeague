# National Armchair League — User Manual

## Table of Contents
1. [Getting Started](#1-getting-started)
2. [Roles & Permissions](#2-roles--permissions)
3. [Dashboard](#3-dashboard)
4. [Making Picks](#4-making-picks)
5. [Standings & Profiles](#5-standings--profiles)
6. [How Scoring Works](#6-how-scoring-works)
7. [Contributor Guide — Spreads & Scores](#7-contributor-guide--spreads--scores)
8. [Admin Guide — Managing the League](#8-admin-guide--managing-the-league)

---

## 1. Getting Started

### Registering
**The NAL is invite-only.** You cannot create an account without an invite code
from an Admin — there is no open sign-up.

An Admin sends you an invite link that looks like
`https://your-league-site/register?code=ABCD-EFGH-JKLM`. Open it and the code is
filled in for you; then enter your first name, last name, email address, and a
password. Your email is what you'll use to log in.

If you were given just the code rather than a link, go to `/register` and type it
into the **Invite Code** box.

Each code works exactly once, and an Admin may lock a code to a specific email
address or give it an expiry date. If your link says the invite is invalid, used,
expired, or revoked, ask an Admin for a fresh one.

> **Note:** On a brand-new install with no accounts at all, the very first person
> to register does so without a code and automatically becomes the Admin.
> Everyone after that needs an invite and starts as a Player.

### Logging In
Go to `/login`, enter your email and password. You'll be kept logged in for 30 days.

---

## 2. Roles & Permissions

There are three roles in NAL:

| Role | What they can do |
|---|---|
| **Player** | Enter picks, view dashboard, standings, and profiles |
| **Contributor** | Everything a Player can do, plus manage spreads and scores |
| **Admin** | Everything a Contributor can do, plus manage seasons, weeks, and users |

Admins can change any user's role from the **Manage Users** page.

---

## 3. Dashboard

The dashboard (`/`) is your home base. It shows:

### This Week's Games
Each game card displays:
- **Teams** — away vs. home, with logos
- **Spread** — see [How Scoring Works](#6-how-scoring-works) for an explanation
- **Kickoff time** — or live score if the game is in progress (auto-refreshes every 60 seconds)
- **Your pick** — highlighted once you've submitted picks for the week:
  - **Green** = correct pick
  - **Red** = wrong pick
  - **Yellow** = game not yet final

### Sidebar — Standings
- **This Week** — current week leaderboard
- **Season** — cumulative season leaderboard

Your row is highlighted in yellow. Click any player's name to view their profile.

---

## 4. Making Picks

Go to **My Picks** in the navigation bar. Picks must be entered before the first game of the week kicks off — after that, picks are locked.

### The Confidence Point System
Every week you assign a unique point value to each game. The number of available points matches the number of games that week (e.g., 16 games = points 1–16).

- **Higher points** = you're more confident in that pick
- Each point value can only be used **once** per week
- If your pick is correct, you earn those points
- If your pick is wrong, you earn **zero**

**Example:** You assign 16 points to the Chiefs and they cover the spread — you earn 16 points. If they don't cover, you earn 0.

### Entering Your Picks
1. For each game, click the team you think will **cover the spread**
2. Assign a confidence point value from the dropdown
3. Click **Save My Picks**

You can edit your picks any time before the lock. After the lock, all players' picks become visible to everyone.

### Important Timing Notes
- **Spreads** may still update up until 24 hours before the first kickoff
- **Picks lock** when the first game of the week begins — you cannot change picks after that
- A warning banner on the picks page shows when the lock is approaching

---

## 5. Standings & Profiles

### Standings Page (`/standings`)
The full season leaderboard with a week-by-week breakdown. Use the season dropdown to view past seasons.

### Player Profiles (`/profile/{id}`)
Click any player's name in the standings or dashboard to see their pick history for any season.

---

## 6. How Scoring Works

NAL uses **against the spread (ATS)** picks — you are not simply picking the winner of the game.

### What is a Spread?
The spread is a point handicap designed to even the playing field between a strong team and a weak one.

- The spread is shown from the **home team's perspective**
- **Negative number** = home team is favored (must win by more than that number)
- **Positive number** = away team is favored

**Example:**
> Chiefs **-6.5** vs. Raiders
>
> The Chiefs must win by **7 or more points** to "cover" the spread.
> - Chiefs win 28–20 (margin: 8) → **Chiefs cover** ✓
> - Chiefs win 24–20 (margin: 4) → **Raiders cover** ✓
> - Chiefs win 27–20 (margin: 7) → **Chiefs cover** ✓ (more than 6.5)

### Pick Evaluation
Once a game is final, the system automatically determines which team covered and scores all picks for that game immediately.

| Result | Points Earned |
|---|---|
| Correct pick | Your assigned confidence points |
| Wrong pick | 0 |
| Game not yet final | Pending (shown in yellow) |

---

## 7. Contributor Guide — Spreads & Scores

Contributors have access to a **Manage** menu in the navigation bar.

### Managing Spreads (`/admin/spreads`)
Spreads are automatically fetched from the ESPN API but can be overridden manually.

- Enter a spread value from the home team's perspective (e.g., `-3.5` = home favored by 3.5)
- Manual overrides are flagged so you know which spreads came from the API vs. were set by hand
- Spreads **lock automatically** 24 hours before the first kickoff and cannot be edited after that

### Managing Scores (`/admin/scores`)
If the automatic ESPN score sync isn't working, scores can be entered manually.

1. Enter the away and home scores for the game
2. Check the **Final** box when the game is complete
3. Click **Save**

Marking a game as final immediately triggers scoring for all picks on that game.

> All spread and score changes are logged in the audit trail.

---

## 8. Admin Guide — Managing the League

Admins have access to the full **Admin Panel** at `/admin/`.

### Season Management
- Create a new season by entering the year (e.g., `2025`)
- Only one season can be **active** at a time — setting a new one active deactivates the previous one
- Past seasons remain in the database and can be viewed from the standings/profile pages

### Week Management
From the admin panel, create weeks within the active season:

| Field | Description |
|---|---|
| **Week Number** | 1–18 for regular season, 19+ for playoffs |
| **Label** | Optional custom name (e.g., "Wild Card", "Super Bowl") |
| **ESPN Week** | The week number used by the ESPN API for schedule syncing |
| **First Kickoff** | When picks will automatically lock |

#### Week Admin Page (`/admin/week/{id}`)
From here you can:
- **Sync from ESPN** — pulls the latest schedule and odds for the week
- **Edit a kickoff time** — click a game's kickoff (the ✎ pencil) to open an inline
  editor. Enter the date and time in **Eastern (ET)** and Save. This is handy when a
  synced schedule has the wrong time. Editing the earliest game also updates the
  week's automatic picks-lock and spread-lock times.
- **Lock Spreads** — manually lock spreads early if needed
- **Lock Picks** — manually lock picks early if needed
- **Edit any player's picks** — useful if a player had a technical issue

> **All game times are shown in US Eastern (ET).** Times are stored internally in
> UTC and converted for display, so daylight-saving changes are handled automatically.

### Invites (`/admin/` → Invites)
Registration is invite-only, so this panel is how new players get in.

- **Create Invite** — generates a single-use code in `XXXX-XXXX-XXXX` form. Options:
  - **Lock to Email** *(optional)* — only that email address can redeem the code.
    Leave blank for a code anyone holding the link can use once.
  - **Note** *(optional)* — a reminder to yourself of who it's for.
  - **Expires** — 7, 14, or 30 days, or never. Defaults to 30 days.
- **Copy link** — copies the full `/register?code=...` link to your clipboard. Send
  that to your player by text or email.
- **Revoke** — kills an unused code immediately, e.g. if a link was forwarded to
  the wrong person.
- **Delete** — removes the row from the list (housekeeping only).

Each invite shows its status: **Active**, **Used** (with who redeemed it),
**Expired**, or **Revoked**. Creating, revoking, and deleting invites are all
recorded in the Audit Log.

> Adding a player directly under **Users** below does not need an invite — that
> path creates the account for them outright.

### User Management (`/admin/users`)
- View all registered users with their email, role, and active status
- **Change role** — promote players to Contributor or Admin using the dropdown
- **Disable/Enable** — disabled users cannot log in (useful if someone leaves the league)
- **Delete** — permanently removes a user and all of their picks. Use this to clean up
  test accounts. This cannot be undone, so a confirmation is required. You cannot delete
  your own account or the only remaining admin.

> You cannot change your own role.

### Audit Log
The bottom of the admin panel shows the last 20 actions taken by admins and contributors — who changed what, and when. All pick edits, spread overrides, score updates, role changes, kickoff edits, user deletions, and submitted issues are recorded here.

---

## 9. Submitting an Issue

Any logged-in user can report a bug or suggestion from the **Submit an Issue** page
(`/feedback`), linked in the top navigation and the page footer. Enter a short title and
a description and submit — the report is filed as an issue on the league's GitHub
repository, with the submitter's name and email attached so admins can follow up.

**Setup (admins):** issue reporting requires two environment variables:

| Variable | Description |
|---|---|
| `GITHUB_ISSUE_TOKEN` | A GitHub token with read/write access to Issues on the repo |
| `GITHUB_ISSUE_REPO` | The `owner/repo` to file issues on (defaults to the project repo) |

Until these are set, the page still loads but tells users that reporting isn't available.

---

## 10. Configuration — Environment Variables

App configuration lives in a `.env` file next to `docker-compose.yml` on the host.
Copy `.env.example` to `.env` (`cp .env.example .env`) and fill it in. After changing
any value, recreate the container so it's picked up — run `./update.sh` (or
`docker compose up -d`); editing `.env` alone does **not** affect a running container.

| Variable | Required? | What it does |
|---|---|---|
| `SECRET_KEY` | No (generated) | Signs the login-session tokens (JWT cookies). Left unset, the container generates a long random key into `data/secret.key` on first boot and reuses it on every update, so nobody is logged out by an update. Set it explicitly only when restoring a backup and you need existing logins to keep working. Changing it logs everyone out once (no data loss). |
| `REGISTRATION_OPEN` | No (default `true`) | Master on/off switch for the **/register** page. Registration is invite-only either way, so `true` is the normal setting — an invite code is still required. Set `false` only if you want to close `/register` outright, blocking even valid invite holders. Admins can still add users manually from the Admin Panel regardless. |
| `ODDS_API_KEY` | No | API key for [The Odds API](https://the-odds-api.com) used to **auto-fetch NFL point spreads**. If blank, auto-fetch is skipped and spreads are entered manually on `/admin/spreads`. |
| `DATABASE_URL` | No (default set) | SQLite database location. The image already defaults to `sqlite:////app/data/nal.db`, which is the mounted `./data` volume, so leave it alone unless you are doing something unusual. |
| `PUID` / `PGID` | No (default `99`/`100`) | User and group the app runs as, and the owner it gives files in `./data`. `99:100` (nobody:users) is the Unraid default and is almost always right. |
| `GITHUB_ISSUE_TOKEN` | No | GitHub token with read/write access to Issues. Enables the **Submit an Issue** feature (Section 9). If blank, the feedback page shows a "not configured" notice. |
| `GITHUB_ISSUE_REPO` | No (defaults to project repo) | The `owner/repo` that user-submitted issues are filed on. |

> **Keep `.env` private.** It holds secrets (signing key, API tokens) and is excluded
> from git via `.gitignore`, so it never gets committed or pulled — you maintain it
> on the host. It won't appear with a plain `ls`; use `ls -a` to see it.

### Updating the app (Unraid / Docker)

Every merge to `main` publishes a ready-built image to the GitHub Container
Registry (`ghcr.io/jeffreyrdubois/nationalarmchairleague:latest`), for both
amd64 and arm64. Updating pulls that image — there is nothing to compile on the
server, so an update takes seconds rather than minutes.

**On Unraid**, the container shows up in the Docker tab with an **update ready**
flag once a new image is published. Click **Apply** and you are done.

To install it that way the first time, add this under
*Docker → Add Container → Template repositories*:

```
https://raw.githubusercontent.com/jeffreyrdubois/NationalArmchairLeague/main/unraid/nal.xml
```

Nothing needs configuring for a first run: click Apply, open the WebUI, and
register the first account. The signing key generates itself and lives in the
data folder, so it survives every future update.

**Anywhere else** (or from the repo directory on the host):

```bash
./update.sh
```

This pulls the new image, restarts the container, prunes old images, and prints
the version that came up. Your database (`./data`) and `.env` are left untouched.

### Checking which version is running

The version appears in the footer of every page, and at `/health`, which needs
no login:

```bash
curl -s http://your-server:5950/health
{"status":"ok","version":"1.0.0+a1b2c3d","built_at":"2026-08-29T14:02:11Z","commit":"a1b2c3d..."}
```

A version ending in `-dev` means the running image was built by hand rather than
published by CI — useful for telling "the update did not apply" apart from "the
update applied and did not fix it".

---

## Quick Reference

| Page | URL | Who |
|---|---|---|
| Dashboard | `/` | All |
| My Picks | `/picks` | All |
| All Picks (after lock) | `/picks/week/{id}/all` | All |
| Standings | `/standings` | All |
| Submit an Issue | `/feedback` | All |
| Spreads | `/admin/spreads` | Contributor+ |
| Scores | `/admin/scores` | Contributor+ |
| Admin Panel | `/admin/` | Admin |
| Week Admin | `/admin/week/{id}` | Admin |
| Users | `/admin/users` | Admin |
