from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.utils import to_eastern

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["enumerate"] = enumerate
templates.env.filters["enumerate"] = enumerate
# Kickoff/lock times are stored as naive UTC but shown to users in Eastern.
# Pipe a datetime through `| eastern` before strftime to render the correct ET
# wall-clock time (e.g. {{ (game.kickoff_time | eastern).strftime('...') }}).
templates.env.filters["eastern"] = to_eastern
