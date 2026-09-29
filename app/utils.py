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


# How a player wants prize money sent. Distinct from the commissioner's own
# Venmo/Zelle/Cash App handles, which are how players pay the league.
PAYMENT_METHODS = (
    ("zelle", "Zelle"),
    ("venmo", "Venmo"),
    ("cashapp", "Cash App"),
)
_PAYMENT_METHOD_LABELS = dict(PAYMENT_METHODS)


def payment_method_label(code: str | None) -> str:
    return _PAYMENT_METHOD_LABELS.get(code or "", "")


def normalize_payment_method(value: str | None) -> str | None:
    """A preferred payout rail, or None when nobody has picked one.

    Blank clears the preference. Anything other than Zelle, Venmo, or Cash
    App is refused — the forms only offer those, so a different value is a
    tampered request rather than a new method to quietly store.
    """
    key = (value or "").strip().casefold()
    if not key:
        return None
    if key not in _PAYMENT_METHOD_LABELS:
        raise ValueError("Pick Zelle, Venmo, or Cash App.")
    return key


def _as_user(person):
    """A User, or a standings/submission row that carries one."""
    if person is None:
        return None
    if isinstance(person, dict):
        user = person.get("user")
        if user is not None and hasattr(user, "first_name"):
            return user
        return None
    nested = getattr(person, "user", None)
    if nested is not None and hasattr(nested, "first_name"):
        return nested
    if hasattr(person, "first_name") and hasattr(person, "id"):
        return person
    return None


def _last_fragment(user, group) -> str:
    """Enough of the last name to tell ``user`` apart from ``group``.

    One letter when that is unique ("D"), more when two people share it
    ("Da" / "Di"). The first letter is capitalised; the rest keeps the
    spelling on the account.
    """
    last = (user.last_name or "").strip()
    if not last:
        return ""
    n = 1
    while n < len(last):
        prefix = last[:n].casefold()
        rivals = [
            other for other in group
            if (other.last_name or "").strip()[:n].casefold() == prefix
        ]
        if len(rivals) == 1:
            break
        n += 1
    shown = last[:n]
    return shown[0].upper() + shown[1:]


def short_labels(people) -> dict[int, str]:
    """How to show each player when the page only has room for a first name.

    A first name that belongs to one person stays as it is: "Jeffrey".
    A first name shared by two or more gains the first letter of the last
    name, so the two Stephens read "Stephen D" and "Stephen M" instead of
    both reading "Stephen". Comparison ignores case. If the last initial
    is shared too, more of the last name is kept until the labels differ.
    """
    users = []
    seen = set()
    for person in people or ():
        user = _as_user(person)
        if user is None or user.id in seen:
            continue
        seen.add(user.id)
        users.append(user)

    groups: dict[str, list] = {}
    for user in users:
        groups.setdefault((user.first_name or "").casefold(), []).append(user)

    labels: dict[int, str] = {}
    for group in groups.values():
        if len(group) == 1:
            labels[group[0].id] = group[0].first_name or ""
            continue
        for user in group:
            fragment = _last_fragment(user, group)
            first = user.first_name or ""
            labels[user.id] = f"{first} {fragment}".strip()
    return labels


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
