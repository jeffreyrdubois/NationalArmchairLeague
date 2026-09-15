"""Prize payouts: how the pool is divided, and who is owed what.

The plan is the only thing stored. No payout is ever snapshotted — every dollar
the league owes is recomputed from the saved amounts against the standings as
they stand right now. That is what makes a mid-season change backdate on its
own: raise first place from $10 to $15 in week 6 and the player who won week 1
is owed $15, with nothing to go back and re-key.

Because of that, the plan is a *budget* as much as a rule set. A plan that pays
$15/$10/$5 a week for 18 weeks plus $250 at season's end has committed $790 of
the pool, and the playground exists so an admin can see that number before the
season is half gone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models import (
    AuditLog, Game, PayoutPlan, PayoutRule, Season, Transaction, User, Week,
)
from app.services.awards import AWARD_REGISTRY, compute_all_awards, rank_award
from app.services.scoring import get_season_standings, get_week_standings
from app.utils import ordinal

WEEKLY = "weekly"
SEASON = "season"
AWARD = "award"

# Regular season length — the default number of weeks a plan budgets for.
DEFAULT_PAID_WEEKS = 18

# How many empty place rows the editor always offers, so adding a fourth
# season-end place does not require reaching for the "add place" button.
MIN_WEEKLY_PLACES = 5
MIN_SEASON_PLACES = 6

# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    """A season's prize structure, detached from the database.

    Held as plain dicts so the playground can build one straight from a form
    and price it without writing anything down.
    """
    pool: float = 0.0
    paid_weeks: int = DEFAULT_PAID_WEEKS
    weekly: dict[int, float] = field(default_factory=dict)   # place → amount
    season: dict[int, float] = field(default_factory=dict)   # place → amount
    awards: dict[str, float] = field(default_factory=dict)   # award_id → amount
    notes: str = ""
    # False until somebody has actually set the prizes — an unconfigured
    # season pays nobody rather than paying everybody nothing.
    is_configured: bool = False

    def amounts_for(self, category: str) -> dict[int, float]:
        return self.weekly if category == WEEKLY else self.season


def _clean(amounts: dict) -> dict:
    """Drop the places and awards set to nothing — they are not prizes."""
    return {k: round(float(v), 2) for k, v in amounts.items() if float(v or 0) > 0}


def load_plan(db: Session, season_id: int) -> Plan:
    """The saved plan for a season, or an empty one with sensible defaults."""
    row = db.query(PayoutPlan).filter(PayoutPlan.season_id == season_id).first()
    if not row:
        return Plan(paid_weeks=default_paid_weeks(db, season_id))

    plan = Plan(
        pool=float(row.pool_amount or 0),
        paid_weeks=int(row.paid_weeks or 0),
        notes=row.notes or "",
        is_configured=True,
    )
    for rule in row.rules:
        if rule.category == WEEKLY:
            plan.weekly[rule.rank] = float(rule.amount or 0)
        elif rule.category == SEASON:
            plan.season[rule.rank] = float(rule.amount or 0)
        elif rule.category == AWARD:
            plan.awards[rule.award_id] = float(rule.amount or 0)
    plan.weekly = _clean(plan.weekly)
    plan.season = _clean(plan.season)
    plan.awards = _clean(plan.awards)
    return plan


def default_paid_weeks(db: Session, season_id: int) -> int:
    """How many regular season weeks this season actually has."""
    count = (
        db.query(Week)
        .filter(Week.season_id == season_id, Week.week_number <= DEFAULT_PAID_WEEKS)
        .count()
    )
    return count or DEFAULT_PAID_WEEKS


def save_plan(db: Session, season: Season, plan: Plan, admin: User) -> PayoutPlan:
    """Write a plan, replacing whatever was there.

    The rules are rewritten wholesale rather than merged: a place removed from
    the form is a place that no longer pays, and leaving a stale row behind
    would quietly keep paying it.
    """
    row = db.query(PayoutPlan).filter(PayoutPlan.season_id == season.id).first()
    if not row:
        row = PayoutPlan(season_id=season.id)
        db.add(row)

    row.pool_amount = round(float(plan.pool or 0), 2)
    row.paid_weeks = max(0, int(plan.paid_weeks or 0))
    row.notes = (plan.notes or "").strip() or None
    row.updated_by_id = admin.id
    db.flush()

    db.query(PayoutRule).filter(PayoutRule.plan_id == row.id).delete(
        synchronize_session=False
    )
    for rank, amount in _clean(plan.weekly).items():
        db.add(PayoutRule(plan_id=row.id, category=WEEKLY, rank=rank, amount=amount))
    for rank, amount in _clean(plan.season).items():
        db.add(PayoutRule(plan_id=row.id, category=SEASON, rank=rank, amount=amount))
    for award_id, amount in _clean(plan.awards).items():
        db.add(PayoutRule(
            plan_id=row.id, category=AWARD, rank=1,
            award_id=award_id, amount=amount,
        ))

    totals = plan_totals(plan)
    db.add(AuditLog(
        user_id=admin.id,
        action="update_payout_plan",
        target_type="season",
        target_id=season.id,
        detail=(
            f"{season.year} pool ${totals['pool']:.2f}, "
            f"${totals['allocated']:.2f} allocated "
            f"(weekly ${totals['weekly_total']:.2f} over {row.paid_weeks} weeks, "
            f"season ${totals['season_total']:.2f}, "
            f"awards ${totals['awards_total']:.2f})"
        ),
    ))
    db.commit()
    return row


def plan_totals(plan: Plan) -> dict:
    """Price a plan against its pool. This is the playground's whole point."""
    weekly_per_week = round(sum(_clean(plan.weekly).values()), 2)
    weekly_total = round(weekly_per_week * max(0, plan.paid_weeks), 2)
    season_total = round(sum(_clean(plan.season).values()), 2)
    awards_total = round(sum(_clean(plan.awards).values()), 2)
    allocated = round(weekly_total + season_total + awards_total, 2)
    remaining = round(float(plan.pool or 0) - allocated, 2)
    return {
        "pool":            round(float(plan.pool or 0), 2),
        "weekly_per_week": weekly_per_week,
        "weekly_total":    weekly_total,
        "season_total":    season_total,
        "awards_total":    awards_total,
        "allocated":       allocated,
        "remaining":       remaining,
        # Cents matter here: "fully allocated" has to mean exactly, or the
        # admin finds out in January that the pool was a dollar short.
        "is_balanced":     abs(remaining) < 0.005,
        "is_over":         remaining < -0.005,
    }


# ---------------------------------------------------------------------------
# Splitting money between tied players
# ---------------------------------------------------------------------------

def split_evenly(total: float, n: int) -> list[float]:
    """Split a dollar amount into n whole-cent shares that add back up exactly.

    Three players tying for a $15/$10/$5 podium share $30 at $10 each; two
    tying for $15/$10 get $12.50 each. When it does not divide evenly the odd
    cents go to the front of the list rather than evaporating.
    """
    if n <= 0:
        return []
    cents = int(round(total * 100))
    base, extra = divmod(cents, n)
    return [(base + (1 if i < extra else 0)) / 100 for i in range(n)]


def allocate(groups: list[list[int]], amounts: dict[int, float]) -> dict[int, float]:
    """Hand out place money to ranked groups of tied user ids.

    ``groups`` is ordered best-first; every id inside a group finished level.
    A tie absorbs the places it spans and splits their combined money, so the
    pool pays out the same total no matter how the week finished.
    """
    payouts: dict[int, float] = {}
    place = 1
    for group in groups:
        size = len(group)
        purse = sum(float(amounts.get(place + i, 0) or 0) for i in range(size))
        if purse > 0:
            # Sorted for determinism: within a tie there is no "first", so the
            # odd cent has to land somewhere repeatable.
            for user_id, share in zip(sorted(group), split_evenly(purse, size)):
                if share:
                    payouts[user_id] = share
        place += size
    return payouts


def _group_by_score(rows: list[dict], key: str) -> list[list[int]]:
    """Turn an ordered standings list into groups of tied user ids."""
    groups: list[list[int]] = []
    last = object()
    for row in rows:
        score = row[key]
        user = row["user"]
        if not user:
            continue
        if score != last:
            groups.append([])
            last = score
        groups[-1].append(user.id)
    return groups


# ---------------------------------------------------------------------------
# Which weeks and seasons have actually finished
# ---------------------------------------------------------------------------

def _week_is_final(db: Session, week: Week) -> bool:
    """A week pays out once every one of its games is final."""
    games = db.query(Game).filter(Game.week_id == week.id).all()
    return bool(games) and all(g.is_final for g in games)


def payable_weeks(db: Session, season_id: int, plan: Plan) -> list[Week]:
    """Finished weeks inside the plan's paid range, in order."""
    weeks = (
        db.query(Week)
        .filter(Week.season_id == season_id, Week.week_number <= plan.paid_weeks)
        .order_by(Week.week_number)
        .all()
    )
    return [w for w in weeks if _week_is_final(db, w)]


def season_is_complete(db: Session, season_id: int, plan: Plan) -> bool:
    """True once the season's prizes can be handed out for real.

    Season-end and award money is the last thing paid, so it stays a projection
    until every week that has games has finished *and* the season has played at
    least as many weeks as the plan budgets for. The second half matters in
    September, when weeks 3-18 may not have been synced yet and "every week
    with games is final" would otherwise declare the season over after week 2.
    """
    weeks = db.query(Week).filter(Week.season_id == season_id).all()
    played = [w for w in weeks if db.query(Game).filter(Game.week_id == w.id).count()]
    if not played:
        return False
    if not all(_week_is_final(db, w) for w in played):
        return False
    return len(played) >= max(plan.paid_weeks, 1)


# ---------------------------------------------------------------------------
# Working out who is owed what
# ---------------------------------------------------------------------------

@dataclass
class PayoutLine:
    """One prize owed to one player."""
    user: User
    category: str
    label: str          # "Week 3", "Season Standings", "Nail Biter"
    place: str          # "1st", or "T-1st" when the place was shared
    amount: float
    # False while the result is still a projection (the season is not over).
    earned: bool = True
    week_number: int | None = None


def _lines_for(
    rows: list[dict],
    score_key: str,
    amounts: dict[int, float],
    users_by_id: dict[int, User],
    category: str,
    label: str,
    earned: bool,
    week_number: int | None = None,
) -> list[PayoutLine]:
    groups = _group_by_score(rows, score_key)
    payouts = allocate(groups, amounts)
    lines: list[PayoutLine] = []
    place = 1
    for group in groups:
        for user_id in sorted(group):
            amount = payouts.get(user_id)
            user = users_by_id.get(user_id)
            if amount and user:
                lines.append(PayoutLine(
                    user=user,
                    category=category,
                    label=label,
                    place=("T-" if len(group) > 1 else "") + ordinal(place),
                    amount=amount,
                    earned=earned,
                    week_number=week_number,
                ))
        place += len(group)
    return lines


@dataclass
class PayoutReport:
    """Everything the funds page and the playground need to show."""
    plan: Plan
    lines: list[PayoutLine] = field(default_factory=list)
    weeks: list[dict] = field(default_factory=list)   # {week, lines, total}
    season_lines: list[PayoutLine] = field(default_factory=list)
    award_lines: list[dict] = field(default_factory=list)  # {config, lines}
    season_complete: bool = False
    weeks_paid: int = 0

    @property
    def earned_total(self) -> float:
        return round(sum(l.amount for l in self.lines if l.earned), 2)

    @property
    def projected_total(self) -> float:
        return round(sum(l.amount for l in self.lines if not l.earned), 2)

    def earned_by_user(self) -> dict[int, float]:
        totals: dict[int, float] = {}
        for line in self.lines:
            if line.earned:
                totals[line.user.id] = round(totals.get(line.user.id, 0) + line.amount, 2)
        return totals

    def projected_by_user(self) -> dict[int, float]:
        totals: dict[int, float] = {}
        for line in self.lines:
            if not line.earned:
                totals[line.user.id] = round(totals.get(line.user.id, 0) + line.amount, 2)
        return totals

    def lines_for_user(self, user_id: int) -> list[PayoutLine]:
        return [l for l in self.lines if l.user.id == user_id]


def compute_payouts(db: Session, season: Season, plan: Plan) -> PayoutReport:
    """Price a plan against a season's actual results.

    Pass a plan straight from the playground form to see what a change would
    have paid out without saving it; pass the saved plan to see what the league
    actually owes.
    """
    report = PayoutReport(plan=plan)
    if not plan.is_configured:
        return report

    users_by_id: dict[int, User] = {u.id: u for u in db.query(User).all()}

    # --- weekly prizes: paid as soon as the week's last game goes final ---
    if plan.weekly:
        for week in payable_weeks(db, season.id, plan):
            standings = get_week_standings(db, week.id)
            week_lines = _lines_for(
                standings, "total", plan.weekly, users_by_id,
                WEEKLY, week.label or f"Week {week.week_number}",
                earned=True, week_number=week.week_number,
            )
            report.weeks.append({
                "week":   week,
                "lines":  week_lines,
                "total":  round(sum(l.amount for l in week_lines), 2),
            })
            report.lines.extend(week_lines)
        report.weeks_paid = len(report.weeks)

    complete = season_is_complete(db, season.id, plan)
    report.season_complete = complete

    # --- season standings: a projection until the last week is in the books ---
    if plan.season:
        report.season_lines = _lines_for(
            get_season_standings(db, season.id), "total", plan.season, users_by_id,
            SEASON, "Season Standings", earned=complete,
        )
        report.lines.extend(report.season_lines)

    # --- awards: same, and an award nobody has scored on pays nobody ---
    if plan.awards:
        all_scores = compute_all_awards(db, season.id)
        active_users = {
            u.id: u for u in users_by_id.values() if u.is_active
        }
        for cfg in AWARD_REGISTRY:
            amount = plan.awards.get(cfg.id, 0)
            if not cfg.enabled or not amount:
                continue
            ranking = rank_award(
                all_scores.get(cfg.id, {}), active_users, cfg.win_condition
            )
            # A score of zero is nobody having done the thing — Bottom Feeder
            # pays nothing until eliminations start, rather than paying whoever
            # happens to sort first on a table of zeroes.
            ranking = [r for r in ranking if r["score"] > 0]
            lines = _lines_for(
                ranking, "score", {1: amount}, active_users,
                AWARD, cfg.name, earned=complete,
            )
            report.award_lines.append({"config": cfg, "amount": amount, "lines": lines})
            report.lines.extend(lines)

    return report


# ---------------------------------------------------------------------------
# Earned vs. logged — the "who do I still owe" view
# ---------------------------------------------------------------------------

def payout_ledger(db: Session, report: PayoutReport, users: list[User]) -> list[dict]:
    """Per player: what they have earned, what has been logged, what is left.

    ``paid`` counts every outgoing transaction, which is the same ledger the
    funds page has always used — log a payout there and it clears here.
    """
    paid_by_user: dict[int, float] = {}
    for txn in db.query(Transaction).filter(Transaction.direction == "out").all():
        paid_by_user[txn.user_id] = paid_by_user.get(txn.user_id, 0) + txn.amount

    earned = report.earned_by_user()
    projected = report.projected_by_user()

    ledger = []
    for user in users:
        earned_amt = round(earned.get(user.id, 0.0), 2)
        paid_amt = round(paid_by_user.get(user.id, 0.0), 2)
        ledger.append({
            "user":      user,
            "earned":    earned_amt,
            "paid":      paid_amt,
            "owed":      round(earned_amt - paid_amt, 2),
            "projected": round(projected.get(user.id, 0.0), 2),
            "lines":     report.lines_for_user(user.id),
        })
    # Whoever is owed the most first — this table exists to be worked down.
    ledger.sort(key=lambda r: (-r["owed"], r["user"].last_name, r["user"].first_name))
    return ledger
