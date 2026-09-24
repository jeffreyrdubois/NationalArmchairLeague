"""OAuth 2.1 for the MCP server: pre-registered clients, codes and tokens.

Claude connects to ``/mcp`` the way it does to any remote connector: it is
given a client ID and secret, sends you to ``/oauth/authorize`` to log in and
approve, then trades the code it gets back for an access token (with PKCE, so
a code that leaks on the way back is useless on its own). The token acts as
the person who approved it and nothing more.

There is no dynamic client registration — the only clients are the ones an
admin creates on the settings page. Everything secret (client secrets, codes,
access and refresh tokens) is stored as a SHA-256 hash.
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

# Who may connect Claude. Admins only for now — the server is the
# commissioner's tool. Widening it is this one line: every tool answers as the
# person who approved the connection and applies the site's visibility rules,
# so a player would see only what that player can.
ALLOWED_ROLES = frozenset({Role.admin})

CODE = "code"
ACCESS = "access"
REFRESH = "refresh"

CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
# Refresh tokens rotate on every use, so this is how long a connection can sit
# completely idle before Claude has to be approved again.
REFRESH_TTL = timedelta(days=90)

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
    client = get_client(db, client_id)
    if not client or not client_secret:
        return None
    if not hmac.compare_digest(client.secret_hash, _hash(client_secret)):
        return None
    return client


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


def disconnect_all(db: Session, client: OAuthClient) -> None:
    """Sign every connected app out without deleting the client."""
    db.query(OAuthToken).filter(OAuthToken.client_id == client.client_id).delete()
    db.commit()
