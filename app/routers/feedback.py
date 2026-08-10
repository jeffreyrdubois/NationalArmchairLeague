"""
User-submitted issue reports.

Any logged-in user can file a report from /feedback. When a GitHub token and
repository are configured (GITHUB_ISSUE_TOKEN / GITHUB_ISSUE_REPO), the report
is opened as an issue on the project's GitHub repo. Without that configuration
the page still works but tells the user reporting is unavailable.
"""
import os
import logging

import httpx
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.templates_config import templates
from app.database import get_db
from app.auth import get_current_user
from app.models import AuditLog

logger = logging.getLogger(__name__)

router = APIRouter()

# Default to this project's repo; override with an env var if it ever moves.
GITHUB_ISSUE_REPO = os.getenv("GITHUB_ISSUE_REPO", "jeffreyrdubois/nationalarmchairleague")
GITHUB_API = "https://api.github.com"


def _issue_reporting_configured() -> bool:
    return bool(os.getenv("GITHUB_ISSUE_TOKEN")) and bool(GITHUB_ISSUE_REPO)


async def _create_github_issue(title: str, body: str) -> tuple[bool, str | None]:
    """Create an issue on the configured repo. Returns (ok, issue_url_or_error)."""
    token = os.getenv("GITHUB_ISSUE_TOKEN")
    if not token or not GITHUB_ISSUE_REPO:
        return False, "Issue reporting is not configured on the server."

    url = f"{GITHUB_API}/repos/{GITHUB_ISSUE_REPO}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json={"title": title, "body": body})
        if resp.status_code == 201:
            return True, resp.json().get("html_url")
        logger.warning(f"GitHub issue creation failed ({resp.status_code}): {resp.text[:300]}")
        return False, f"GitHub returned an error ({resp.status_code}). Please try again later."
    except Exception as e:
        logger.error(f"GitHub issue creation error: {e}")
        return False, "Could not reach GitHub. Please try again later."


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        "feedback/submit.html",
        {
            "request": request,
            "user": user,
            "configured": _issue_reporting_configured(),
            "submitted": request.query_params.get("submitted"),
            "issue_url": request.query_params.get("issue_url"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/feedback")
async def submit_feedback(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    from urllib.parse import quote

    title = title.strip()
    description = description.strip()
    if not title or not description:
        return RedirectResponse(
            url="/feedback?error=" + quote("Please add both a title and a description."),
            status_code=303,
        )

    issue_title = f"[User report] {title}"
    issue_body = (
        f"{description}\n\n"
        f"---\n"
        f"_Submitted from the National Armchair League app by "
        f"{user.full_name} ({user.email})._"
    )

    ok, result = await _create_github_issue(issue_title, issue_body)

    # Record the submission regardless of GitHub outcome.
    db.add(AuditLog(
        user_id=user.id,
        action="submit_issue",
        target_type="feedback",
        target_id=None,
        detail=(f"Filed issue: {title}" + (f" ({result})" if ok else " — GitHub unavailable")),
    ))
    db.commit()

    if ok:
        return RedirectResponse(
            url="/feedback?submitted=1&issue_url=" + quote(result or ""),
            status_code=303,
        )
    return RedirectResponse(
        url="/feedback?error=" + quote(result or "Something went wrong."),
        status_code=303,
    )
