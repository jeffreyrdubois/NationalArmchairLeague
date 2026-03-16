from fastapi.templating import Jinja2Templates
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["enumerate"] = enumerate
templates.env.filters["enumerate"] = enumerate
