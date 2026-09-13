"""
User-submitted issue reports.

Any logged-in user can file a report from /feedback. When a GitHub token and
repository are configured — in the Admin Panel, or failing that via the
GITHUB_ISSUE_TOKEN / GITHUB_ISSUE_REPO environment variables — the report is
opened as an issue on the project's GitHub repo. Without that configuration
the page still works but tells the user reporting is unavailable.
"""
import logging

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session

from app.templates_config import templates
from app.database import get_db
from app.auth import get_current_user
from app.models import AuditLog, Role
from app.services import github_issues

logger = logging.getLogger(__name__)

router = APIRouter()


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
            "configured": github_issues.is_configured(db),
            "is_admin": user.role == Role.admin,
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

    ok, result = await github_issues.create_issue(db, issue_title, issue_body)

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
