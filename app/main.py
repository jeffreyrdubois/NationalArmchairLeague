from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
from contextlib import asynccontextmanager
from pathlib import Path
import logging
import os

from app.database import init_db
from app.routers import auth, picks, dashboard, admin, awards
from app.routers import push, feedback
from app.services.scheduler import setup_scheduler, scheduler
from app.services.notifications import init_vapid_keys
from app.templates_config import templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")
    init_vapid_keys()
    setup_scheduler()
    yield
    scheduler.shutdown()
    logger.info("Scheduler shut down")


app = FastAPI(title="National Armchair League", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(picks.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(awards.router)
app.include_router(push.router)
app.include_router(feedback.router)


# Build metadata, baked in by the publish workflow (see .github/workflows/
# docker-publish.yml). An image built by hand reports the "-dev" default, so a
# local build can never be mistaken here for a published one.
APP_VERSION = os.getenv("APP_VERSION", "0.0.0-dev")
BUILD_DATE = os.getenv("BUILD_DATE", "")
GIT_COMMIT = os.getenv("GIT_COMMIT", "")


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe and build identity.

    Unauthenticated on purpose: Docker's HEALTHCHECK has no session, and after
    an Unraid update the first question is "did the new image actually take?" —
    which needs an answer you can get before logging in.
    """
    return JSONResponse(
        {
            "status": "ok",
            "version": APP_VERSION,
            "built_at": BUILD_DATE,
            "commit": GIT_COMMIT,
        }
    )


# Custom 401/403 → redirect to login
@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return templates.TemplateResponse(
        "base.html",
        {"request": request, "user": None},
        status_code=403,
    )
