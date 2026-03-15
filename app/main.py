from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from pathlib import Path
import logging

from app.database import init_db
from app.routers import auth, picks, dashboard, admin
from app.services.scheduler import setup_scheduler, scheduler
from app.templates_config import templates

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialized")
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
