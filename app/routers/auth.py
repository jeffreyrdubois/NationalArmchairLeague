from app.templates_config import templates
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse

from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Role
from app.auth import hash_password, verify_password, create_access_token, get_current_user
import os

router = APIRouter()


REGISTRATION_OPEN = os.getenv("REGISTRATION_OPEN", "true").lower() == "true"


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
    return templates.TemplateResponse("auth/register.html", {"request": request, "error": None})


@router.post("/register")
async def register(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(get_db),
):
    if not REGISTRATION_OPEN:
        raise HTTPException(status_code=403, detail="Registration closed")

    def render_error(msg):
        return templates.TemplateResponse(
            "auth/register.html",
            {"request": request, "error": msg},
            status_code=400,
        )

    if password != password2:
        return render_error("Passwords do not match")
    if db.query(User).filter(User.email == email).first():
        return render_error("Email already registered")

    try:
        is_first = db.query(User).count() == 0
        user = User(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            email=email,
            password_hash=hash_password(password),
            role=Role.admin if is_first else Role.player,
        )
        db.add(user)
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
