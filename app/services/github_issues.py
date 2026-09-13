"""
"Submit an Issue" -> GitHub.

The token and repository live in the app_settings table so an admin can set
them from the Admin Panel, without editing the Docker config and restarting
the container. Environment variables (GITHUB_ISSUE_TOKEN / GITHUB_ISSUE_REPO)
still work and are used whenever nothing is stored in the database, so an
existing deployment keeps working untouched — save a value in the admin page
and it takes over from there.

The token is a credential: it is stored as-is because the app needs to send it
to GitHub, but it is never rendered back to a page, never written to the audit
log, and never included in an error message.
"""
import logging
import os

import httpx
from sqlalchemy.orm import Session

from app.models import AppSetting

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

SETTING_TOKEN = "github_issue_token"
SETTING_REPO = "github_issue_repo"

# Where reports go when nothing has been configured anywhere.
DEFAULT_REPO = "jeffreyrdubois/nationalarmchairleague"


def _stored(db: Session) -> dict:
    rows = (
        db.query(AppSetting)
        .filter(AppSetting.key.in_([SETTING_TOKEN, SETTING_REPO]))
        .all()
    )
    return {row.key: (row.value or "").strip() for row in rows}


def get_config(db: Session) -> dict:
    """Resolve the effective token/repo, database first, environment second.

    ``token_source`` / ``repo_source`` say where each value came from, so the
    admin page can tell "saved here" from "coming from the container config".
    """
    stored = _stored(db)

    env_token = (os.getenv("GITHUB_ISSUE_TOKEN") or "").strip()
    env_repo = (os.getenv("GITHUB_ISSUE_REPO") or "").strip()

    token = stored.get(SETTING_TOKEN) or env_token
    repo = stored.get(SETTING_REPO) or env_repo or DEFAULT_REPO

    return {
        "token": token,
        "repo": repo,
        "token_source": "app" if stored.get(SETTING_TOKEN) else ("env" if env_token else None),
        "repo_source": "app" if stored.get(SETTING_REPO) else ("env" if env_repo else "default"),
        "configured": bool(token and repo),
    }


def is_configured(db: Session) -> bool:
    return get_config(db)["configured"]


def mask_token(token: str) -> str:
    """A hint that the right token is saved, without showing it."""
    if not token:
        return ""
    if len(token) <= 8:
        return "•" * len(token)
    return f"{token[:4]}{'•' * 8}{token[-4:]}"


def save_config(db: Session, token: str | None, repo: str | None) -> None:
    """Store token and/or repo. A blank token leaves any saved token alone.

    That blank-means-keep rule is what lets an admin correct the repository
    without having to paste the token in again (the page never shows it, so
    re-typing it is not something they could do accurately anyway).
    """
    token = (token or "").strip()
    repo = (repo or "").strip()

    if token:
        db.merge(AppSetting(key=SETTING_TOKEN, value=token))
    if repo:
        db.merge(AppSetting(key=SETTING_REPO, value=repo))
    db.commit()


def clear_config(db: Session) -> None:
    """Remove the stored token and repo, falling back to the environment."""
    (
        db.query(AppSetting)
        .filter(AppSetting.key.in_([SETTING_TOKEN, SETTING_REPO]))
        .delete(synchronize_session=False)
    )
    db.commit()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def verify(token: str, repo: str) -> tuple[bool, str]:
    """Check that the token can see the repo and that it has Issues enabled.

    Returns (ok, message) — the message is shown to the admin either way, so
    it says what to fix rather than just that something failed.
    """
    if not token or not repo:
        return False, "Both a repository and a token are required."
    if repo.count("/") != 1 or not all(part.strip() for part in repo.split("/")):
        return False, f"“{repo}” is not a valid repository — use the owner/name form."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{GITHUB_API}/repos/{repo}", headers=_headers(token))
    except Exception as e:
        logger.error(f"GitHub verification error: {e}")
        return False, "Could not reach GitHub. Check the server's network access and try again."

    if resp.status_code == 200:
        if resp.json().get("has_issues") is False:
            return False, f"Connected to {repo}, but that repository has Issues turned off."
        return True, f"Connected to {repo}."
    if resp.status_code in (401, 403):
        return False, "GitHub rejected the token. Check that it has read/write access to Issues on this repo."
    if resp.status_code == 404:
        return False, f"GitHub could not find {repo} with this token — check the owner/name and the token's repository access."
    logger.warning(f"GitHub verification failed ({resp.status_code}): {resp.text[:300]}")
    return False, f"GitHub returned an unexpected error ({resp.status_code})."


async def create_issue(db: Session, title: str, body: str) -> tuple[bool, str | None]:
    """Create an issue on the configured repo. Returns (ok, issue_url_or_error)."""
    config = get_config(db)
    token, repo = config["token"], config["repo"]
    if not config["configured"]:
        return False, "Issue reporting is not configured yet — an admin can set it up in the Admin Panel."

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{GITHUB_API}/repos/{repo}/issues",
                headers=_headers(token),
                json={"title": title, "body": body},
            )
        if resp.status_code == 201:
            return True, resp.json().get("html_url")
        logger.warning(f"GitHub issue creation failed ({resp.status_code}): {resp.text[:300]}")
        return False, f"GitHub returned an error ({resp.status_code}). Please try again later."
    except Exception as e:
        logger.error(f"GitHub issue creation error: {e}")
        return False, "Could not reach GitHub. Please try again later."
