"""Fire a signed webhook a configurable time before the week's first kickoff.

The league used to ask an outside automation to guess when picks were about to
lock (a fixed Thursday evening). Kickoff moves. This posts once per week, at
the offset an admin chose under Account Settings -> AI Access, so the receiver
runs when the window is actually open.

The body is HMAC-SHA256 signed with the saved secret. A delivery is marked
sent only after a 2xx, so a failed post is retried on the next scheduler tick
until kickoff. It never fires twice for the same week.
"""
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models import AppSetting, Season, Week

logger = logging.getLogger(__name__)

SETTING_URL = "picks_webhook_url"
SETTING_SECRET = "picks_webhook_secret"
SETTING_OFFSET = "picks_webhook_offset_minutes"
SETTING_STATUS = "picks_webhook_status"

DEFAULT_OFFSET_MINUTES = 60
OFFSET_CHOICES = (
    (15, "15 minutes"),
    (30, "30 minutes"),
    (60, "1 hour"),
    (90, "90 minutes"),
    (120, "2 hours"),
    (180, "3 hours"),
    (360, "6 hours"),
    (720, "12 hours"),
    (1440, "24 hours"),
)
_ALLOWED_OFFSETS = {minutes for minutes, _ in OFFSET_CHOICES}

EVENT = "picks_lock_approaching"
MAX_ATTEMPTS = 3


def _get(db: Session, key: str) -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return (row.value if row else "") or ""


def offset_minutes(db: Session) -> int:
    raw = _get(db, SETTING_OFFSET).strip()
    try:
        minutes = int(raw)
    except ValueError:
        return DEFAULT_OFFSET_MINUTES
    return minutes if minutes in _ALLOWED_OFFSETS else DEFAULT_OFFSET_MINUTES


def config(db: Session) -> dict:
    url = _get(db, SETTING_URL).strip()
    secret = _get(db, SETTING_SECRET)
    return {
        "url": url,
        "secret_set": bool(secret),
        "secret_hint": ("••••" + secret[-4:]) if len(secret) >= 4 else ("set" if secret else ""),
        "offset_minutes": offset_minutes(db),
        "offsets": [{"minutes": m, "label": label} for m, label in OFFSET_CHOICES],
        "enabled": bool(url),
    }


def save_config(db: Session, url: str, secret: str | None, minutes: int) -> str | None:
    """Persist the form. A blank secret keeps the current one.

    Returns a newly generated secret the one time it is readable, or None.
    """
    url = (url or "").strip()
    if url and not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError("The webhook URL has to start with http:// or https://.")
    if minutes not in _ALLOWED_OFFSETS:
        raise ValueError("Pick one of the listed lead times.")

    generated = None
    existing = _get(db, SETTING_SECRET)
    if secret:
        stored = secret.strip()
    elif url and not existing:
        stored = secrets.token_hex(32)
        generated = stored
    else:
        stored = existing

    db.merge(AppSetting(key=SETTING_URL, value=url))
    db.merge(AppSetting(key=SETTING_SECRET, value=stored))
    db.merge(AppSetting(key=SETTING_OFFSET, value=str(minutes)))
    db.commit()
    return generated


def status(db: Session) -> dict | None:
    raw = _get(db, SETTING_STATUS)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def next_delivery(db: Session, now: datetime | None = None) -> dict | None:
    """The next week this would fire for, if a URL is set and kickoff is known."""
    if not config(db)["enabled"]:
        return None
    now = now or datetime.utcnow()
    offset = timedelta(minutes=offset_minutes(db))
    weeks = (
        db.query(Week)
        .join(Season)
        .filter(Season.is_active.is_(True), Week.is_completed.is_(False))
        .order_by(Week.week_number)
        .all()
    )
    for week in weeks:
        kickoff = week.first_kickoff
        if kickoff is None or kickoff <= now or week.picks_webhook_sent:
            continue
        return {
            "season_year": week.season.year,
            "week_number": week.week_number,
            "label": week.label,
            "first_kickoff": kickoff,
            "fires_at": kickoff - offset,
        }
    return None


def sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(week: Week, minutes: int, fired_at: datetime) -> dict:
    kickoff = week.first_kickoff
    return {
        "event": EVENT,
        "season_year": week.season.year,
        "week_number": week.week_number,
        "week_label": week.label,
        "first_kickoff": kickoff.replace(microsecond=0).isoformat() + "Z",
        "offset_minutes": minutes,
        "fired_at": fired_at.replace(microsecond=0).isoformat() + "Z",
    }


def due_weeks(db: Session, now: datetime) -> list[Week]:
    offset = timedelta(minutes=offset_minutes(db))
    weeks = (
        db.query(Week)
        .join(Season)
        .filter(Season.is_active.is_(True), Week.is_completed.is_(False))
        .all()
    )
    ready = []
    for week in weeks:
        kickoff = week.first_kickoff
        if kickoff is None or week.picks_webhook_sent:
            continue
        if kickoff - offset <= now < kickoff:
            ready.append(week)
    return ready


async def post_webhook(url: str, secret: str, body: bytes, delivery_id: str) -> tuple[bool, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "NationalArmchairLeague-webhook",
        "X-NAL-Event": EVENT,
        "X-NAL-Delivery": delivery_id,
        "X-NAL-Signature": sign(secret, body),
    }
    last = "no attempt"
    async with httpx.AsyncClient(timeout=15) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"attempt {attempt}: {exc}"
                logger.warning("Picks webhook %s", last)
                continue
            if 200 <= resp.status_code < 300:
                return True, f"HTTP {resp.status_code}"
            last = f"attempt {attempt}: HTTP {resp.status_code}"
            logger.warning("Picks webhook %s", last)
    return False, last


def _record(db: Session, payload: dict) -> None:
    db.merge(AppSetting(key=SETTING_STATUS, value=json.dumps(payload)))


async def fire_due(db: Session, now: datetime | None = None, poster=post_webhook) -> int:
    """POST every week whose lead time has arrived. Returns how many succeeded."""
    if not config(db)["enabled"]:
        return 0
    now = now or datetime.utcnow()
    secret = _get(db, SETTING_SECRET)
    url = _get(db, SETTING_URL).strip()
    minutes = offset_minutes(db)
    sent = 0
    for week in due_weeks(db, now):
        payload = build_payload(week, minutes, now)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        delivery_id = f"{payload['season_year']}-W{payload['week_number']}"
        ok, detail = await poster(url, secret, body, delivery_id)
        _record(db, {
            "delivery_id": delivery_id,
            "ok": ok,
            "detail": detail,
            "fired_at": payload["fired_at"],
            "week_number": payload["week_number"],
            "season_year": payload["season_year"],
        })
        if ok:
            week.picks_webhook_sent = True
            sent += 1
            logger.info("Picks webhook delivered for %s", delivery_id)
        else:
            logger.error("Picks webhook failed for %s: %s", delivery_id, detail)
    db.commit()
    return sent
