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
9. [Money — Prize Payouts & League Funds](#9-money--prize-payouts--league-funds)
10. [Submitting an Issue](#10-submitting-an-issue)
11. [Configuration — Environment Variables](#11-configuration--environment-variables)

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
| **Admin** | Everything a Contributor can do, plus manage seasons, weeks, users, and the league's money |

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

You can edit your picks any time before the lock.

### Who Can See Your Picks
Nobody — not other players, and not the commissioner — can see your picks
before the week locks. That includes the standings page, player profiles and
the all-picks page. Everyone's picks become visible to everyone at the lock,
and not a moment earlier.

What everyone *can* see beforehand is **who has submitted**. The standings page
and `/picks/week/{id}/all` both show a roster for the current week marking each
player as submitted, partway, or not started, so you can chase whoever still
owes their picks. It is names and counts only — never a team or a point value.

The one exception is the admin panel's **Edit User Picks** screen, which an
admin uses to enter picks on behalf of a player who sent them in by text. Every
such edit is recorded in the admin audit log.

### Reading the All Picks Grid
After the lock, `/picks/week/{id}/all` shows every player's picks as one grid —
one row per game, one column per player. It is wider than a phone screen, so the
first four columns are frozen: the game, the spread, the result and **your own
picks**, which are pulled to the front and tinted. Scroll sideways and those four
stay put while the other players slide past, so you are always comparing against
your own column. The row of player names and the totals row stay put as you
scroll up and down.

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
The page opens on the week currently being played; use the week buttons for any other week.

1. Enter the away and home scores for the game
2. Check the **Final** box when the game is complete
3. Click **Save**

Marking a game as final immediately triggers scoring for all picks on that game, and correcting
a score on a game that is already final re-scores those picks from the new score.

A final you type in is flagged **Manual** and the automatic sync will not overwrite it. A score saved
without **Final** is provisional, so the live sync may still refine it. Either way the sync only fills
in games it actually has scores for — it never blanks a score or un-finals a finished game.
To hand a game back to the sync, use **Clear score** (which also resets that game's picks to pending).

> A game can be saved with only one score filled in — it just can't be marked final until both are there.

> All spread and score changes are logged in the audit trail.

#### Sync Scores

**Sync Scores** pulls the feed for the week you are looking at, the same way the background sync does
every five minutes, and reports what happened right on the page:

- **live from ESPN** — real-time scores, including games in progress
- **nflverse (finished games only)** — ESPN was unreachable, so scores appear once a game is over
- **no feed answered** — neither source returned anything for this week; enter scores by hand

The line also says how many of the week's games the feed matched. A week that reads
*matched 0 of 16* is the tell that the feed is answering but its games aren't lining up with the ones
in the database — the sync now re-matches those by team and repairs them on the next pass.

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

## 9. Money — Prize Payouts & League Funds

Two admin-only pages work together. **Prize Payouts** (`/admin/payouts`) decides how the
season's pool is divided up. **League Funds** (`/admin/funds`) tracks the money actually
moving — entry fees in, prizes out — and tells you who is still waiting to be paid.

### Prize Payouts (`/admin/payouts`)

Enter the **Total Pool** (e.g. `$1,000`) and how many **Weeks Paid** the weekly prizes run
for (18 by default), then fill in three sets of amounts:

| Section | When it pays | Notes |
|---|---|---|
| **Weekly Prizes** | Every week, as soon as that week's last game goes final | Set an amount per place — 1st `$15`, 2nd `$10`, 3rd `$5` |
| **Season End Prizes** | Once, on the final season standings | e.g. top 4 |
| **Award Prizes** | Once, to whoever leads each award at season's end | One prize per award |

The dark bar at the top keeps a running total as you type: what the plan commits, what is
left of the pool, and whether you have gone over. It turns green on **Fully allocated ✓**
when the pool is spent to the cent.

Two buttons:

- **Preview Without Saving** — prices the numbers you have typed against the season's real
  results. This is the playground: try `$15/$10/$5` weekly against `$20/$10` and see what
  each would have paid out so far before committing to either.
- **Save Plan** — stores it.

> **Changes are backdated, always.** No payout is ever frozen in place — every amount on
> both pages is worked out from the current plan against the standings each time the page
> loads. Decide in week 6 that first place is worth `$15` rather than `$10`, and the player
> who won week 1 is owed `$15`. There is nothing to go back and re-enter.

**Ties split the places they span.** Two players tied for first share the 1st and 2nd
prizes at `$12.50` each, and third place still collects 3rd. The pool pays out the same
total however the week finishes, down to the cent.

Season end and award money stays a **projection** until the season is over — it is shown
so you can see where things are heading, but it is not counted as owed. An award nobody
has scored on (Bottom Feeder, before eliminations start) pays nothing.

### League Funds (`/admin/funds`)

**Settings** holds the entry fee and your payment handles (Venmo, PayPal, Cash App, Zelle).

**Payouts To Make** is the one to check each week. It lists every player with what they
have **earned** under the prize plan, what you have **paid** them, and what is still
**owed** — plus a *What For* column breaking the total down by week and place. The red
banner at the top is the short version: "3 players are waiting on a total of $30.00".

- **Log $15.00** next to a player records that payment in one click.
- **Log all 3 payouts** does the whole round at once.

Both only write the payment down — the money still leaves by Venmo or by hand.

**Weekly Prize Winners** lists each finished week's winners and amounts, most recent
first, so you can see at a glance who to pay this week.

**Player Status** covers the other direction — who has paid their entry fee — and the
**Transaction Log** is every movement in and out, with a Delete on each row if you log
something by mistake.

---

## 10. Submitting an Issue

Any logged-in user can report a bug or suggestion from the **Submit an Issue** page
(`/feedback`), linked in the top navigation and the page footer. Enter a short title and
a description and submit — the report is filed as an issue on the league's GitHub
repository, with the submitter's name and email attached so admins can follow up.

**Setup (admins):** open the Admin Panel and find **Issue Reporting (GitHub)**. Enter
the repository (`owner/repo`) and a GitHub
[fine-grained personal access token](https://github.com/settings/personal-access-tokens)
with **read and write access to Issues** on that repository, then **Save & Verify**.
The settings are checked against GitHub before they are saved, so a typo is reported
straight away rather than silently swallowing every report. Nothing needs to go into
`.env` and the container does not need restarting.

Once saved, the panel shows the repository and a masked hint of the token — the token
itself is never displayed again, so to change it you paste a new one (leaving the token
box blank keeps the saved one, which is how you correct the repository on its own).
**Test Connection** re-checks the saved settings, and **Clear Saved Settings** removes
them.

The `GITHUB_ISSUE_TOKEN` / `GITHUB_ISSUE_REPO` environment variables (Section 10) still
work and are used when nothing is saved in the app; anything saved in the Admin Panel
takes precedence. Until one or the other is set, the page still loads but tells users
that reporting isn't available.

---

## 11. Configuration — Environment Variables

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
| `GITHUB_ISSUE_TOKEN` | No | GitHub token with read/write access to Issues, enabling the **Submit an Issue** feature (Section 9). Optional even for that: setting it in the Admin Panel instead is the easier route, and a value saved there wins over this one. |
| `GITHUB_ISSUE_REPO` | No (defaults to project repo) | The `owner/repo` that user-submitted issues are filed on. Also settable in the Admin Panel, which takes precedence. |

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
| All Picks (after lock) / Pick Status (before) | `/picks/week/{id}/all` | All |
| Standings | `/standings` | All |
| Submit an Issue | `/feedback` | All |
| Spreads | `/admin/spreads` | Contributor+ |
| Scores | `/admin/scores` | Contributor+ |
| Admin Panel | `/admin/` | Admin |
| Prize Payouts | `/admin/payouts` | Admin |
| League Funds | `/admin/funds` | Admin |
| Week Admin | `/admin/week/{id}` | Admin |
| Users | `/admin/users` | Admin |
