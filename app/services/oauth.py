"""OAuth 2.1 for the MCP server: clients, codes and tokens.

An AI app connects to ``/mcp`` the way it does to any remote connector: it
gets a client ID (registering itself at ``/oauth/register``, or using one an
admin created on the settings page), sends you to ``/oauth/authorize`` to log
in and approve, then trades the code it gets back for an access token (with
PKCE, so a code that leaks on the way back is useless on its own). The token
acts as the person who approved it and nothing more.

Registration is open — that is what lets any league member connect whatever AI
they use with just the server URL — because a client on its own gets nothing:
every token needs a member to log in and click Allow, and a code only ever goes
to a redirect URI the client registered. Everything secret (client secrets,
codes, access and refresh tokens) is stored as a SHA-256 hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.models import OAuthClient, OAuthToken, Role, User

# Who may connect an AI app: everyone in the league. Every tool answers as the
# person who approved the connection and applies the site's visibility rules,
# so a player sees only what that player can on the site (no league money, no
# one else's picks before the lock).
ALLOWED_ROLES = frozenset(Role)

CODE = "code"
ACCESS = "access"
REFRESH = "refresh"

CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
# Refresh tokens rotate on every use, so this is how long a connection can sit
# completely idle before Claude has to be approved again.
REFRESH_TTL = timedelta(days=90)

# Self-registration housekeeping. A registration nobody approved within a day
# is dropped, as is one whose every token has expired; past the cap, new
# registrations are refused until old ones are cleared out.
UNUSED_REGISTRATION_TTL = timedelta(days=1)
MAX_SELF_REGISTERED = 500

AUTH_METHODS = ("none", "client_secret_post", "client_secret_basic")

# Where Claude sends the browser back after approval: the claude.ai / Claude
# app callbacks, and Claude Code's local listener (any port — see
# redirect_uri_allowed). Prefilled for a new client; editable on the settings
# page.
DEFAULT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost/callback",
    "http://127.0.0.1/callback",
]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def may_use_mcp(user: User | None) -> bool:
    return bool(user and user.is_active and user.role in ALLOWED_ROLES)


def may_manage_clients(user: User | None) -> bool:
    """Admins create and manage pre-registered clients for everyone."""
    return bool(may_use_mcp(user) and user.role == Role.admin)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def create_client(
    db: Session, admin: User, name: str, redirect_uris: list[str],
) -> tuple[OAuthClient, str]:
    """Create a client; returns it and its secret, readable this once only."""
    secret = "nalsec_" + secrets.token_urlsafe(32)
    client = OAuthClient(
        client_id="nal_" + secrets.token_urlsafe(12),
        secret_hash=_hash(secret),
        name=(name or "Claude").strip()[:100],
        redirect_uris="\n".join(redirect_uris or DEFAULT_REDIRECT_URIS),
        created_by_id=admin.id,
    )
    db.add(client)
    db.commit()
    return client, secret


def rotate_secret(db: Session, client: OAuthClient) -> str:
    secret = "nalsec_" + secrets.token_urlsafe(32)
    client.secret_hash = _hash(secret)
    db.commit()
    return secret


def delete_client(db: Session, client: OAuthClient) -> None:
    """Remove a client and every token it was ever given."""
    db.query(OAuthToken).filter(OAuthToken.client_id == client.client_id).delete()
    db.delete(client)
    db.commit()


def get_client(db: Session, client_id: str | None) -> OAuthClient | None:
    if not client_id:
        return None
    return db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()


def authenticate_client(
    db: Session, client_id: str | None, client_secret: str | None,
) -> OAuthClient | None:
    """The client, if it proved who it is: its secret, or for a public
    client (which has none) its client ID alone — PKCE does the rest."""
    client = get_client(db, client_id)
    if not client:
        return None
    if client.is_public:
        return client
    if not client_secret or not hmac.compare_digest(client.secret_hash, _hash(client_secret)):
        return None
    return client


class RegistrationError(Exception):
    """An RFC 7591 registration error: ``error`` is the error code."""

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error
        self.description = description


def prune_registrations(db: Session) -> None:
    """Drop self-registered clients nobody is using any more."""
    now = datetime.utcnow()
    live = {
        client_id for (client_id,) in
        db.query(OAuthToken.client_id).filter(OAuthToken.expires_at > now).distinct()
    }
    stale = (
        db.query(OAuthClient)
        .filter(
            OAuthClient.self_registered.is_(True),
            OAuthClient.created_at < now - UNUSED_REGISTRATION_TTL,
        )
        .all()
    )
    for client in stale:
        if client.client_id not in live:
            db.query(OAuthToken).filter(OAuthToken.client_id == client.client_id).delete()
            db.delete(client)
    db.commit()


def register_client(db: Session, metadata: dict) -> dict:
    """RFC 7591 dynamic registration. Returns the registration response,
    including the secret for a confidential client (readable this once)."""
    if not isinstance(metadata, dict):
        raise RegistrationError("invalid_client_metadata", "Expected a JSON object.")

    uris = metadata.get("redirect_uris")
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise RegistrationError("invalid_redirect_uri", "redirect_uris must be a list of URIs.")
    try:
        uris = parse_redirect_uris("\n".join(uris))
    except ValueError as exc:
        raise RegistrationError("invalid_redirect_uri", str(exc))

    method = metadata.get("token_endpoint_auth_method") or "client_secret_basic"
    if method not in AUTH_METHODS:
        raise RegistrationError(
            "invalid_client_metadata", f"Unsupported token_endpoint_auth_method: {method}")
    grants = metadata.get("grant_types") or ["authorization_code"]
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        raise RegistrationError(
            "invalid_client_metadata", "Only authorization_code and refresh_token are supported.")
    responses = metadata.get("response_types") or ["code"]
    if responses != ["code"]:
        raise RegistrationError("invalid_client_metadata", "Only response_type code is supported.")

    name = metadata.get("client_name")
    name = name.strip()[:100] if isinstance(name, str) and name.strip() else "AI assistant"

    prune_registrations(db)
    count = db.query(OAuthClient).filter(OAuthClient.self_registered.is_(True)).count()
    if count >= MAX_SELF_REGISTERED:
        raise RegistrationError(
            "invalid_client_metadata", "Too many registered clients; try again later.")

    secret = None if method == "none" else "nalsec_" + secrets.token_urlsafe(32)
    client = OAuthClient(
        client_id="nal_" + secrets.token_urlsafe(12),
        secret_hash=_hash(secret) if secret else None,
        name=name,
        redirect_uris="\n".join(uris),
        self_registered=True,
    )
    db.add(client)
    db.commit()

    body = {
        "client_id": client.client_id,
        "client_id_issued_at": int(datetime.utcnow().timestamp()),
        "client_name": client.name,
        "redirect_uris": uris,
        "token_endpoint_auth_method": method,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }
    if secret:
        body["client_secret"] = secret
        body["client_secret_expires_at"] = 0  # never
    return body


def parse_redirect_uris(text: str) -> list[str]:
    """Validate the settings form's redirect list: https, or http to loopback."""
    uris = []
    for line in (text or "").splitlines():
        uri = line.strip()
        if not uri:
            continue
        parts = urlsplit(uri)
        loopback = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if not (parts.scheme == "https" or loopback) or not parts.netloc or parts.fragment:
            raise ValueError(f"Not a usable redirect URI: {uri}")
        uris.append(uri)
    if not uris:
        raise ValueError("At least one redirect URI is required.")
    return uris


def redirect_uri_allowed(client: OAuthClient, uri: str | None) -> bool:
    """Exact match — except that a loopback URI may use any port (RFC 8252),
    since a local client picks a free one each time it runs."""
    if not uri:
        return False
    if uri in client.redirect_uri_list:
        return True
    asked = urlsplit(uri)
    if asked.scheme != "http" or asked.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    for registered in client.redirect_uri_list:
        allowed = urlsplit(registered)
        if (
            allowed.scheme == "http"
            and allowed.hostname == asked.hostname
            and allowed.path == asked.path
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Codes and tokens
# ---------------------------------------------------------------------------

def _issue(db: Session, kind: str, client: OAuthClient, user: User, ttl: timedelta, **extra) -> str:
    value = secrets.token_urlsafe(32)
    db.add(OAuthToken(
        kind=kind,
        token_hash=_hash(value),
        client_id=client.client_id,
        user_id=user.id,
        expires_at=datetime.utcnow() + ttl,
        **extra,
    ))
    return value


def _find(db: Session, kind: str, value: str | None) -> OAuthToken | None:
    if not value:
        return None
    row = (
        db.query(OAuthToken)
        .filter(OAuthToken.kind == kind, OAuthToken.token_hash == _hash(value))
        .first()
    )
    if row and row.expires_at <= datetime.utcnow():
        db.delete(row)
        db.commit()
        return None
    return row


def issue_code(
    db: Session, client: OAuthClient, user: User, redirect_uri: str, code_challenge: str,
) -> str:
    code = _issue(db, CODE, client, user, CODE_TTL,
                  code_challenge=code_challenge, redirect_uri=redirect_uri)
    db.commit()
    return code


def pkce_matches(verifier: str | None, challenge: str | None) -> bool:
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


class GrantError(Exception):
    """An OAuth token-endpoint error: ``error`` is the RFC 6749 code."""

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error
        self.description = description


def _token_pair(db: Session, client: OAuthClient, user: User) -> dict:
    access = _issue(db, ACCESS, client, user, ACCESS_TTL)
    refresh = _issue(db, REFRESH, client, user, REFRESH_TTL)
    client.last_used_at = datetime.utcnow()
    db.commit()
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TTL.total_seconds()),
        "refresh_token": refresh,
    }


def exchange_code(
    db: Session, client: OAuthClient, code: str | None,
    redirect_uri: str | None, code_verifier: str | None,
) -> dict:
    row = _find(db, CODE, code)
    if not row or row.client_id != client.client_id:
        raise GrantError("invalid_grant", "The authorization code is invalid or expired.")
    user, issued_for, challenge = row.user, row.redirect_uri, row.code_challenge
    # Single use, whatever happens next.
    db.delete(row)
    db.commit()
    if issued_for != redirect_uri:
        raise GrantError("invalid_grant", "redirect_uri does not match the authorization request.")
    if not pkce_matches(code_verifier, challenge):
        raise GrantError("invalid_grant", "PKCE verification failed.")
    if not may_use_mcp(user):
        raise GrantError("invalid_grant", "This account may not connect to the MCP server.")
    return _token_pair(db, client, user)


def exchange_refresh(db: Session, client: OAuthClient, refresh_token: str | None) -> dict:
    row = _find(db, REFRESH, refresh_token)
    if not row or row.client_id != client.client_id:
        raise GrantError("invalid_grant", "The refresh token is invalid or expired.")
    user = row.user
    db.delete(row)  # rotated: each refresh token works once
    db.commit()
    if not may_use_mcp(user):
        raise GrantError("invalid_grant", "This account may not connect to the MCP server.")
    return _token_pair(db, client, user)


def revoke(db: Session, client: OAuthClient, token: str | None) -> None:
    """RFC 7009: revoke an access or refresh token; unknown tokens are fine."""
    if not token:
        return
    db.query(OAuthToken).filter(
        OAuthToken.token_hash == _hash(token),
        OAuthToken.client_id == client.client_id,
        OAuthToken.kind.in_([ACCESS, REFRESH]),
    ).delete(synchronize_session=False)
    db.commit()


def user_for_access_token(db: Session, token: str | None) -> User | None:
    """The user an access token acts as, or None if it should be refused."""
    row = _find(db, ACCESS, token)
    if not row or not may_use_mcp(row.user):
        return None
    return row.user


def connections(db: Session, client: OAuthClient) -> int:
    """How many live refresh tokens (connected apps) a client has."""
    return (
        db.query(OAuthToken)
        .filter(
            OAuthToken.client_id == client.client_id,
            OAuthToken.kind == REFRESH,
            OAuthToken.expires_at > datetime.utcnow(),
        )
        .count()
    )


def user_connections(db: Session, user: User) -> list[dict]:
    """The apps a person has connected: one row per client with a live
    refresh token of theirs, newest first."""
    rows = (
        db.query(OAuthToken)
        .filter(
            OAuthToken.user_id == user.id,
            OAuthToken.kind == REFRESH,
            OAuthToken.expires_at > datetime.utcnow(),
        )
        .order_by(OAuthToken.created_at.desc())
        .all()
    )
    seen: dict[str, dict] = {}
    for row in rows:
        if row.client_id in seen:
            continue
        client = get_client(db, row.client_id)
        if client:
            seen[row.client_id] = {"client": client, "last_active": row.created_at}
    return list(seen.values())


def disconnect_user(db: Session, user: User, client_id: str) -> None:
    """Sign one person's connection through one client out."""
    db.query(OAuthToken).filter(
        OAuthToken.user_id == user.id, OAuthToken.client_id == client_id,
    ).delete()
    db.commit()


def disconnect_all(db: Session, client: OAuthClient) -> None:
    """Sign every connected app out without deleting the client."""
    db.query(OAuthToken).filter(OAuthToken.client_id == client.client_id).delete()
    db.commit()
