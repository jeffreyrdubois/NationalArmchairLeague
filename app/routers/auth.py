from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Role, Invite, normalize_invite_code
from app.auth import hash_password, verify_password, create_access_token, get_current_user
import os

router = APIRouter()


# Master switch. Registration is invite-only regardless; setting this to false
# closes the /register page entirely, even to holders of a valid invite.
REGISTRATION_OPEN = os.getenv("REGISTRATION_OPEN", "true").lower() == "true"


def _is_bootstrap(db: Session) -> bool:
    """True on a fresh install with no accounts yet.

    The very first account can be created without an invite (there is nobody
    around to issue one) and becomes the admin.
    """
    return db.query(User).count() == 0


def _lookup_invite(db: Session, code: str) -> Optional[Invite]:
    normalized = normalize_invite_code(code)
    if not normalized:
        return None
    return db.query(Invite).filter(Invite.code == normalized).first()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse("auth/login.html", {"request": request, "error": None})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    def login_error(msg):
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": msg},
            status_code=401,
        )

    try:
        user = db.query(User).filter(User.email == email).first()
        valid = user and verify_password(password, user.password_hash)
    except Exception as e:
        return login_error(f"Login error: {e}")

    if not valid:
        return login_error("Invalid email or password")
    if not user.is_active:
        return login_error("Account is disabled")
    token = create_access_token(user.id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: Session = Depends(get_db)):
    if not REGISTRATION_OPEN:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Registration is currently closed."},
        )
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/", status_code=303)

    bootstrap = _is_bootstrap(db)
    code = request.query_params.get("code", "")
    invite = None if bootstrap else _lookup_invite(db, code)

    error = None
    if code and not bootstrap:
        # Give people arriving on a stale link the bad news up front rather
        # than after they've filled the whole form in.
        if invite is None:
            error = "That invite code isn't valid."
        elif not invite.is_valid:
            error = f"That invite has already been {invite.status}."

    return templates.TemplateResponse(
        "auth/register.html",
        {
            "request": request,
            "error": error,
            "bootstrap": bootstrap,
            "invite_code": normalize_invite_code(code),
            "invite_email": invite.email if invite and invite.is_valid else None,
        },
    )


@router.post("/register")
async def register(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    invite_code: str = Form(""),
    db: Session = Depends(get_db),
):
    if not REGISTRATION_OPEN:
        raise HTTPException(status_code=403, detail="Registration closed")

    bootstrap = _is_bootstrap(db)
    normalized_code = normalize_invite_code(invite_code)

    def render_error(msg):
        return templates.TemplateResponse(
            "auth/register.html",
            {
                "request": request,
                "error": msg,
                "bootstrap": bootstrap,
                "invite_code": normalized_code,
                "invite_email": None,
                "first_name": first_name,
                "last_name": last_name,
                "email": email,
            },
            status_code=400,
        )

    email = email.strip().lower()

    invite = None
    if not bootstrap:
        if not normalized_code:
            return render_error("An invite code is required to create an account.")
        invite = _lookup_invite(db, normalized_code)
        # Same message for unknown and unusable codes: an outsider guessing at
        # codes learns nothing about which ones exist.
        if invite is None or not invite.is_valid:
            return render_error("That invite code isn't valid or has already been used.")
        if invite.email and invite.email.strip().lower() != email:
            return render_error("This invite was issued to a different email address.")

    if password != password2:
        return render_error("Passwords do not match")
    if db.query(User).filter(User.email == email).first():
        return render_error("Email already registered")

    try:
        user = User(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email,
            password_hash=hash_password(password),
            role=Role.admin if bootstrap else Role.player,
        )
        db.add(user)
        db.flush()

        if invite is not None:
            # Re-check under the same transaction so two simultaneous
            # redemptions of one code can't both succeed.
            claimed = (
                db.query(Invite)
                .filter(Invite.id == invite.id, Invite.used_by_id.is_(None))
                .update(
                    {"used_by_id": user.id, "used_at": datetime.utcnow()},
                    synchronize_session=False,
                )
            )
            if not claimed:
                db.rollback()
                return render_error("That invite code isn't valid or has already been used.")

        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        return render_error(f"Registration failed: {e}")

    token = create_access_token(user.id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("access_token")
    return response
