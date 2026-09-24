"""Small shared helpers."""
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def to_eastern(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert a stored kickoff/lock datetime to US Eastern for display.

    Datetimes are persisted as naive UTC (see app.services.espn), while the UI
    presents game times in Eastern.  This treats a naive value as UTC and
    returns a timezone-aware Eastern datetime; strftime on the result renders
    the correct local (ET) wall-clock time, handling EDT/EST automatically.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(EASTERN)


def eastern_to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convert a naive Eastern wall-clock datetime (e.g. from an admin form) to the
    naive UTC value used for storage.  Inverse of ``to_eastern``.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EASTERN)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


_ORDINAL_SUFFIX = {1: "st", 2: "nd", 3: "rd"}


def ordinal(n: int) -> str:
    """1 → '1st', 2 → '2nd', 11 → '11th'. Used for finishing places."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if 10 <= (n % 100) <= 20:
        return f"{n}th"
    return f"{n}{_ORDINAL_SUFFIX.get(n % 10, 'th')}"


def public_url(request, path: str) -> str:
    """An absolute URL for ``path`` as the outside world reaches this app.

    The Host header survives the reverse proxy, but the proxy speaks plain
    http to the app, so the scheme the client actually used comes from
    X-Forwarded-Proto. Used wherever a URL is handed to something outside the
    browser — the OAuth metadata Claude reads, the settings page's copy/paste
    values.
    """
    url = request.base_url
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if proto in ("http", "https"):
        url = url.replace(scheme=proto)
    return str(url).rstrip("/") + path
