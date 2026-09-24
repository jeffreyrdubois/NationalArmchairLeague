"""Personal access tokens for the MCP server.

A token is the MCP server's stand-in for a login cookie: it acts as one user
and nothing more, so every tool answers as that user would see the site —
their own picks before the lock, the admin-only money pages only for an admin.

Only a hash is stored. The token is shown once, when it is issued; issuing a
new one replaces the old, and a token whose owner is deactivated (or loses the
role that may hold one) stops working on the next request.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import McpToken, Role, User

# Who may hold a token. Admins only for now — the server is a personal tool
# for the commissioner. Widening it is this one line: every tool already
# answers as the token's owner and applies the same visibility rules as the
# site, so a player's token would see only what that player can.
ALLOWED_ROLES = frozenset({Role.admin})

# Recognisable in a config file or a leaked paste, the way "ghp_" is.
TOKEN_PREFIX = "nal_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def may_hold_token(user: User | None) -> bool:
    return bool(user and user.is_active and user.role in ALLOWED_ROLES)


def get_token(db: Session, user: User) -> McpToken | None:
    return db.query(McpToken).filter(McpToken.user_id == user.id).first()


def issue_token(db: Session, user: User) -> str:
    """Create (or replace) the user's token and return it — the only time it
    is ever readable."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = get_token(db, user)
    if not row:
        row = McpToken(user_id=user.id)
        db.add(row)
    row.token_hash = hash_token(token)
    row.token_prefix = token[:len(TOKEN_PREFIX) + 4]
    row.created_at = datetime.utcnow()
    row.last_used_at = None
    db.commit()
    return token


def revoke_token(db: Session, user: User) -> bool:
    row = get_token(db, user)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def user_for_token(db: Session, token: str | None) -> User | None:
    """The user a presented token acts as, or None if it should be refused."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    row = (
        db.query(McpToken)
        .filter(McpToken.token_hash == hash_token(token))
        .first()
    )
    if not row or not may_hold_token(row.user):
        return None
    row.last_used_at = datetime.utcnow()
    db.commit()
    return row.user
