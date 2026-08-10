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
