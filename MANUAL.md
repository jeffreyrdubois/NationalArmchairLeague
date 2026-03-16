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
Navigate to `/register` and fill in your first name, last name, email address, and a password. Your email is what you'll use to log in.

> **Note:** The first person to register automatically becomes the Admin. Everyone after that starts as a Player.

Registration can be closed by the Admin once all players have signed up (via the `REGISTRATION_OPEN` environment setting).

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
- **Lock Spreads** — manually lock spreads early if needed
- **Lock Picks** — manually lock picks early if needed
- **Edit any player's picks** — useful if a player had a technical issue

### User Management (`/admin/users`)
- View all registered users with their email, role, and active status
- **Change role** — promote players to Contributor or Admin using the dropdown
- **Disable/Enable** — disabled users cannot log in (useful if someone leaves the league)

> You cannot change your own role.

### Audit Log
The bottom of the admin panel shows the last 20 actions taken by admins and contributors — who changed what, and when. All pick edits, spread overrides, score updates, and role changes are recorded here.

---

## Quick Reference

| Page | URL | Who |
|---|---|---|
| Dashboard | `/` | All |
| My Picks | `/picks` | All |
| All Picks (after lock) | `/picks/week/{id}/all` | All |
| Standings | `/standings` | All |
| Spreads | `/admin/spreads` | Contributor+ |
| Scores | `/admin/scores` | Contributor+ |
| Admin Panel | `/admin/` | Admin |
| Week Admin | `/admin/week/{id}` | Admin |
| Users | `/admin/users` | Admin |
